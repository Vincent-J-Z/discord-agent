#!/usr/bin/env python3
"""Sub-agent manager — long-lived background workers that outlive one @-invocation.

Each @-mention runs as a single `claude -p` with a ~30-min cap and no memory
beyond what it reads back. To run work that's longer than that, or to fan a task
out and keep tabs on it across invocations, spawn it as a SUB-AGENT inside a
named **tmux** session: the session keeps running after this `claude -p` exits,
and a later invocation can list it, read its output, send it input, or reap it.

State for each sub-agent lives in `/workspace/subagents/<name>.json` (survives
container restarts), so a fresh agent run can rediscover what's running and why.

Usage:
  subagent.py spawn <name> <command...>   # start a detached tmux session
      [--cwd DIR] [--channel CH] [--note "what/why"] [--report]
  subagent.py claude <name> "<prompt>"    # spawn a claude sub-agent
      [--cwd DIR] [--channel CH] [--note ...] [--model M] [--interactive]
      # --interactive runs a live claude REPL in the tmux pane (a STEERABLE
      # worker): later `send <name> "<msg>"` reads to it as a new user message
      # (course-correct, add constraints, `send <name> "/exit"` to finish).
      # Without it, the sub-agent is one-shot `claude -p` (fire-and-forget).
  subagent.py list                        # all sessions + status (running/done)
  subagent.py logs <name> [--lines N]     # last N lines of the session's output
  subagent.py send <name> "<keys>"        # type into the session (steer it)
  subagent.py wait <name> [--timeout S]   # block until it exits (for short waits)
  subagent.py kill <name>                 # stop + clean up
  subagent.py reap                        # drop state for sessions that ended

Conventions:
- Names are kebab-case task ids, e.g. `video-backfill`, `beta-backtest`.
- `--channel` records where to report; with `--report` a wrapper posts a Discord
  line via /app/src/discord_api.py when the command finishes (exit code included).
- Long jobs: spawn, tell the user "started, will report when done", and check
  `list`/`logs` on a later invocation. Don't block this run waiting.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get(
    "DISCORD_AGENT_WORKSPACE", os.path.expanduser("~/discordAgentWorkspace")
)
# Persist under /workspace so a fresh container/invocation can rediscover jobs.
STATE_DIR = os.path.join(WORKSPACE, "subagents")
LOG_DIR = os.path.join(WORKSPACE, "logs")
SESSION_PREFIX = "sa-"  # tmux session name = SESSION_PREFIX + <name>


def _ensure_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def _sess(name):
    return SESSION_PREFIX + name


def _state_path(name):
    return os.path.join(STATE_DIR, name + ".json")


def _log_path(name):
    return os.path.join(LOG_DIR, "subagent-" + name + ".log")


def _tmux(*args, check=False, capture=True):
    return subprocess.run(
        ["tmux", *args],
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=check,
    )


def _alive(name):
    return _tmux("has-session", "-t", _sess(name)).returncode == 0


def _load_state(name):
    try:
        with open(_state_path(name)) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(name, **fields):
    _ensure_dirs()
    st = _load_state(name)
    st.update(fields)
    st["name"] = name
    tmp = _state_path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, _state_path(name))


def _session_path(name):
    return os.path.join(STATE_DIR, name + ".session")


# Auth/config env a claude worker needs but a persistent tmux server won't have.
_FORWARD_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR",
                "CLAUDE_BIN", "CLAUDE_MODEL", "AGENT_NAME", "DISCORD_BOT_TOKEN",
                "SLACK_BOT_TOKEN", "DISCORD_AGENT_WORKSPACE")


def _write_runenv(path):
    _ensure_dirs()
    src = dict(os.environ)
    # Fallback: fill any missing auth vars from the workspace .env, so a worker
    # authenticates even when the caller's env is incomplete (the tmux server's
    # env, a sweep, etc.). os.environ still wins where present.
    envfile = os.path.join(WORKSPACE, ".env")
    if os.path.exists(envfile):
        try:
            from dotenv import dotenv_values
            for k, v in dotenv_values(envfile).items():
                if v is not None:
                    src.setdefault(k, v)
        except Exception:
            pass
    lines = [f"{k}={shlex.quote(src[k])}" for k in _FORWARD_ENV if src.get(k)]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_runner(command, name, channel, report, claude_json=False):
    """Write a runner script that records the command's exit code and optionally
    posts a result line to the channel on finish. tmux runs `bash <script>` —
    keeping the body in a file avoids all the quoting pitfalls.
    claude_json=True: the command is a headless `claude -p --output-format json`;
    the runner captures its session_id (for later `steer`/resume) and reports the
    parsed `result` text rather than a raw log tail."""
    log = _log_path(name)
    done = os.path.join(STATE_DIR, name + ".exit")
    # tmux is a persistent daemon: a new session inherits the env the tmux SERVER
    # first started with — which lacks CLAUDE_CODE_OAUTH_TOKEN, so the worker's
    # `claude` would be "Not logged in". Materialize the auth env this process
    # holds into a per-worker file and source it first (mode 600; workspace is
    # already where all secrets live).
    runenv = os.path.join(STATE_DIR, name + ".runenv")
    _write_runenv(runenv)
    lines = [
        "#!/bin/bash",
        f"set -a; [ -f {shlex.quote(runenv)} ] && . {shlex.quote(runenv)}; set +a",
        f"({command}) > {shlex.quote(log)} 2>&1",
        f"echo $? > {shlex.quote(done)}",
    ]
    if claude_json:
        sess = _session_path(name)
        # Pull session_id (so a follow-up can --resume this worker) and the result.
        lines.append(
            f"python3 -c \"import json,sys;"
            f"d=json.load(open({_pyq(log)}));"
            f"open({_pyq(sess)},'w').write(d.get('session_id') or '');"
            f"open({_pyq(log)}+'.result','w').write(d.get('result') or '')\" 2>/dev/null || true"
        )
    if report and channel:
        # Platform-aware: Slack channel ids are C…/D…/G… (alnum), Discord ids are
        # numeric snowflakes — report through the matching toolbox, else the post
        # silently 404s and the job's result never reaches the user.
        ch = str(channel)
        tool = "discord_api.py" if ch.isdigit() else "slack_api.py"
        api = shlex.quote(os.path.join(ROOT, tool))
        prefix = shlex.quote(f"🤖 worker `{name}` finished (exit ")
        if claude_json:
            # Report the actual answer text; fall back to a log tail if empty.
            body_src = (f"BODY=$(cut -c1-1600 {shlex.quote(log)}.result 2>/dev/null); "
                        f"[ -z \"$BODY\" ] && BODY=$(tail -n 3 {shlex.quote(log)} 2>/dev/null "
                        f"| tr '\\n\\r\\t' '   ' | cut -c1-300)")
        else:
            body_src = (f"BODY=$(tail -n 3 {shlex.quote(log)} 2>/dev/null "
                        f"| tr '\\n\\r\\t' '   ' | cut -c1-300)")
        lines += [
            f"EXIT=$(cat {shlex.quote(done)} 2>/dev/null)",
            body_src,
            f'MSG="$(printf \'%s%s):\\n%s\' {prefix} "$EXIT" "$BODY")"',
            f'python {api} post {shlex.quote(str(channel))} "$MSG" >/dev/null 2>&1 || true',
        ]
    lines.append("sleep 2")  # keep the pane briefly so `logs` works right after exit
    path = os.path.join(STATE_DIR, name + ".sh")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _pyq(s):
    """Quote a path for embedding inside a python -c "..." string (shell-double-quoted)."""
    return "'" + str(s).replace("'", "\\'") + "'"


def cmd_spawn(a):
    _ensure_dirs()
    name = a.name
    if _alive(name):
        print(f"error: sub-agent '{name}' already running (kill it first)", file=sys.stderr)
        return 1
    command = a.command
    # Reset any stale exit marker.
    try:
        os.remove(os.path.join(STATE_DIR, name + ".exit"))
    except OSError:
        pass
    runner = _write_runner(command, name, a.channel, a.report,
                           claude_json=getattr(a, "_claude_json", False))
    cwd = a.cwd or ROOT
    r = _tmux("new-session", "-d", "-s", _sess(name), "-c", cwd, "bash", runner)
    if r.returncode != 0:
        print(f"error: tmux failed: {r.stdout}", file=sys.stderr)
        return 1
    _save_state(
        name, command=command, cwd=cwd, channel=a.channel, note=a.note,
        report=bool(a.report), started_at=int(time.time()), kind="shell",
    )
    print(f"spawned sub-agent '{name}' in tmux session {_sess(name)}")
    print(f"  logs: subagent.py logs {name}    list: subagent.py list")
    return 0


def _claude_cmd(prompt, model, resume=None):
    bin_ = os.environ.get("CLAUDE_BIN", "claude")
    parts = [bin_, "-p", "--output-format", "json", "--permission-mode",
             os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")]
    if model:
        parts += ["--model", model]
    if resume:
        parts += ["--resume", resume]
    parts.append(prompt)
    return " ".join(shlex.quote(p) for p in parts)


def cmd_claude(a):
    """Dispatch a headless claude WORKER (`claude -p`). It runs to completion in
    tmux, captures its session_id, and (with --report) posts the result to the
    channel. Give it a real brief. To course-correct after it finishes a round,
    `steer <name> "<follow-up>"` resumes the SAME session with your new message."""
    a.command = _claude_cmd(a.prompt, a.model)
    a._claude_json = True
    rc = cmd_spawn(a)
    if rc == 0:
        _save_state(a.name, kind="claude", prompt=a.prompt, model=a.model)
    return rc


def cmd_steer(a):
    """Course-correct a claude worker: resume its stored session with a follow-up
    message, so it continues with full memory of what it already did. Use this
    (not `send`, which only reaches live TUI/shell jobs) to adjust a claude
    worker between rounds."""
    st = _load_state(a.name)
    try:
        sid = open(_session_path(a.name)).read().strip()
    except Exception:
        sid = ""
    if not sid:
        print(f"error: no stored session for '{a.name}' (has it finished a round "
              f"yet? only claude workers can be steered)", file=sys.stderr)
        return 1
    if _alive(a.name):
        print(f"error: '{a.name}' is still running — wait for the round to finish "
              f"(`logs {a.name}`), then steer", file=sys.stderr)
        return 1
    channel = a.channel or st.get("channel")
    report = a.report or st.get("report")
    a.command = _claude_cmd(a.follow_up, a.model or st.get("model", ""), resume=sid)
    a.channel, a.report, a.note, a.cwd = channel, report, st.get("note", ""), st.get("cwd")
    a._claude_json = True
    for p in (_state_path(a.name), os.path.join(STATE_DIR, a.name + ".exit")):
        pass  # keep state; spawn resets the exit marker itself
    rc = cmd_spawn(a)
    if rc == 0:
        _save_state(a.name, kind="claude", prompt=a.follow_up, model=a.model or st.get("model", ""),
                    channel=channel, note=st.get("note", ""))
        print(f"steered '{a.name}' (resumed session {sid[:8]}…)")
    return rc


def _status(name):
    if _alive(name):
        return "running"
    exit_marker = os.path.join(STATE_DIR, name + ".exit")
    if os.path.exists(exit_marker):
        try:
            return "done(exit " + open(exit_marker).read().strip() + ")"
        except Exception:
            return "done"
    return "ended"


def cmd_list(a):
    _ensure_dirs()
    names = sorted(f[:-5] for f in os.listdir(STATE_DIR) if f.endswith(".json"))
    if not names:
        print("(no sub-agents tracked)")
        return 0
    for name in names:
        st = _load_state(name)
        age = ""
        if st.get("started_at"):
            age = f"{int((time.time() - st['started_at']) // 60)}m ago"
        line = f"{name:24} {_status(name):16} {age:10}"
        if st.get("note"):
            line += f"  — {st['note']}"
        print(line)
        if st.get("kind") == "claude" and st.get("prompt"):
            print(f"{'':24} claude: {st['prompt'][:80]}")
    return 0


def cmd_logs(a):
    # Prefer the on-disk log (full history); fall back to the live pane.
    log = _log_path(a.name)
    if os.path.exists(log):
        out = subprocess.run(["tail", "-n", str(a.lines), log],
                             text=True, stdout=subprocess.PIPE).stdout
        print(out, end="")
        return 0
    if _alive(a.name):
        # Include scrollback so interactive (REPL) workers show real history.
        r = _tmux("capture-pane", "-p", "-S", f"-{max(a.lines, 200)}", "-t", _sess(a.name))
        out = [ln for ln in r.stdout.splitlines() if ln.strip()]
        print("\n".join(out[-a.lines:]))
        return 0
    print(f"(no logs for '{a.name}')", file=sys.stderr)
    return 1


def cmd_send(a):
    if not _alive(a.name):
        print(f"error: '{a.name}' is not running", file=sys.stderr)
        return 1
    # Send the literal text followed by Enter so you can drive an interactive job.
    _tmux("send-keys", "-t", _sess(a.name), a.keys, "Enter")
    print(f"sent to '{a.name}': {a.keys}")
    return 0


def cmd_wait(a):
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if not _alive(a.name):
            print(_status(a.name))
            return 0
        time.sleep(2)
    print(f"still running after {a.timeout}s (use `logs`/`list` later)")
    return 2


def cmd_kill(a):
    if _alive(a.name):
        _tmux("kill-session", "-t", _sess(a.name))
    _save_state(a.name, killed_at=int(time.time()))
    print(f"killed '{a.name}'")
    return 0


def cmd_reap(a):
    _ensure_dirs()
    reaped = []
    for f in os.listdir(STATE_DIR):
        if not f.endswith(".json"):
            continue
        name = f[:-5]
        if not _alive(name):
            for p in (_state_path(name), os.path.join(STATE_DIR, name + ".exit")):
                try:
                    os.remove(p)
                except OSError:
                    pass
            reaped.append(name)
    print("reaped: " + (", ".join(reaped) if reaped else "(none)"))
    return 0


def main():
    p = argparse.ArgumentParser(description="Sub-agent manager (tmux-backed).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="start a detached shell command")
    sp.add_argument("name")
    sp.add_argument("command", help="the shell command, as ONE quoted string")
    sp.add_argument("--cwd")
    sp.add_argument("--channel")
    sp.add_argument("--note", default="")
    sp.add_argument("--report", action="store_true")
    sp.set_defaults(func=cmd_spawn)

    sc = sub.add_parser("claude", help="dispatch a headless claude worker (claude -p)")
    sc.add_argument("name")
    sc.add_argument("prompt", help="the task brief for the worker")
    sc.add_argument("--cwd")
    sc.add_argument("--channel")
    sc.add_argument("--note", default="")
    sc.add_argument("--model", default="")
    sc.add_argument("--report", action="store_true")
    sc.set_defaults(func=cmd_claude)

    stp = sub.add_parser("steer", help="course-correct a finished claude worker (resume its session)")
    stp.add_argument("name")
    stp.add_argument("follow_up", help="your follow-up / correction, as one quoted string")
    stp.add_argument("--channel")
    stp.add_argument("--model", default="")
    stp.add_argument("--report", action="store_true")
    stp.set_defaults(func=cmd_steer)

    sl = sub.add_parser("list", help="list tracked sub-agents + status")
    sl.set_defaults(func=cmd_list)

    sg = sub.add_parser("logs", help="show recent output")
    sg.add_argument("name")
    sg.add_argument("--lines", type=int, default=40)
    sg.set_defaults(func=cmd_logs)

    ss = sub.add_parser("send", help="type input into a running session")
    ss.add_argument("name")
    ss.add_argument("keys")
    ss.set_defaults(func=cmd_send)

    sw = sub.add_parser("wait", help="block until it exits")
    sw.add_argument("name")
    sw.add_argument("--timeout", type=int, default=60)
    sw.set_defaults(func=cmd_wait)

    sk = sub.add_parser("kill", help="stop + record")
    sk.add_argument("name")
    sk.set_defaults(func=cmd_kill)

    sr = sub.add_parser("reap", help="drop state for ended sessions")
    sr.set_defaults(func=cmd_reap)

    a = p.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
