"""Dispatcher narration of finished workers.

A background worker (subagent.py … --report) does NOT talk to the user directly.
When it finishes it drops a report in /workspace/.worker_reports/<name>.json. The
runtime wakes THIS script, which — for each pending report — has the channel
agent (the dispatcher) read the worker's raw output and tell the user the outcome
in its OWN voice: one assistant, one voice. The worker's job was to do the work
and report back to the dispatcher; the dispatcher owns the conversation.

Triggered by discord_agent_runtime when reports are pending (and not rate-limited).
"""
import glob
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

import discord_claude_bridge as b

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", os.path.expanduser("~/discordAgentWorkspace"))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
load_dotenv(os.path.join(WORKSPACE, "secrets.env"), override=True)

REPORTS_DIR = os.path.join(WORKSPACE, ".worker_reports")
# Backstop thresholds: once narrate() (claude -p digesting the result) has
# failed this many times, or the report has sat in the queue this long, give
# up on narration and post the raw result directly instead (see backstop_post).
REPORT_NARRATE_MAX_ATTEMPTS = int(os.environ.get("REPORT_NARRATE_MAX_ATTEMPTS", "3"))
REPORT_BACKSTOP_AGE = int(os.environ.get("REPORT_BACKSTOP_AGE", "600"))


def _instruction(rep):
    channel = str(rep.get("channel"))
    slack = not channel.isdigit()
    toolbox = "slack_api.py" if slack else "discord_api.py"
    plat = "Slack" if slack else "Discord"
    return (
        f"You are {b.AGENT_NAME}. Earlier you dispatched a background worker to do "
        "a task and told the user you'd report back. The worker has now FINISHED "
        "and handed you its raw output (below). Speak to the user in YOUR OWN "
        "voice — do not paste the raw log; digest it and tell them what they need: "
        "the outcome/result, whether it succeeded (exit code), anything notable, "
        "and any decision now needed from them. Be concise and natural, as a "
        "continuation of your earlier conversation.\n"
        f"Post your message to {plat} channel {channel} with:\n"
        f"    python /app/src/{toolbox} post {channel} \"<your message>\"\n"
        "Post exactly once. Your stdout is only logged.\n\n"
        f"Worker task: {rep.get('note') or '(no note)'}\n"
        f"Worker name: {rep.get('name')}\n"
        f"Exit code: {rep.get('exit')}\n"
        f"Worker raw output:\n{rep.get('result') or '(empty)'}"
    )


def narrate(rep):
    channel = str(rep.get("channel"))
    # Continue the dispatcher's own conversation for this channel where possible:
    # Slack sessions are keyed "slack:<ch>", Discord by the channel id.
    session_key = f"slack:{channel}" if not channel.isdigit() else channel
    guild_id = "_slack" if not channel.isdigit() else (b.CHANNEL_GUILD.get(channel) or "_report")
    server_dir = b.ensure_server_dir(guild_id)
    session_id = b.get_session(session_key)
    env = dict(os.environ,
               AGENT_CURRENT_GUILD="" if not channel.isdigit() else str(guild_id),
               AGENT_SERVER_DIR=server_dir,
               CLAUDE_CONFIG_DIR=os.path.join(server_dir, ".claude"),
               TMPDIR=os.path.join(server_dir, "tmp"))
    cmd = [b.CLAUDE_BIN, "-p", "--permission-mode", b.PERMISSION_MODE, "--output-format", "json"]
    if b.CLAUDE_MODEL:
        cmd += ["--model", b.CLAUDE_MODEL]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(_instruction(rep))

    def run(c):
        return subprocess.run(c, cwd=server_dir, text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=b.TIMEOUT_SECONDS)

    r = run(cmd)
    if r.returncode != 0 and session_id:  # stale session → retry fresh
        b.clear_session(session_key)
        r = run([c for c in cmd if c not in (session_id, "--resume")])
    if r.returncode == 0:
        try:
            data = json.loads(r.stdout or "{}")
            if data.get("session_id"):
                b.set_session(session_key, data["session_id"])
        except Exception:
            pass
        print(f"[report] narrated worker '{rep.get('name')}' to {channel}", flush=True)
        return True
    print(f"[report] narration failed for '{rep.get('name')}': "
          f"{(r.stderr or r.stdout)[:200]}", flush=True)
    return False


def _backstop_due(rep):
    """True once narrate() has failed too many times, or the report has aged
    past the backstop window — either way further narrate attempts aren't
    worth it and we fall back to a direct post (see backstop_post), which
    works even while rate-limited since it never calls claude -p."""
    if rep.get("attempts", 0) >= REPORT_NARRATE_MAX_ATTEMPTS:
        return True
    try:
        ts = float(rep.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0.0:
        return False
    return (time.time() - ts) >= REPORT_BACKSTOP_AGE


def backstop_post(rep):
    """Last-resort delivery: post the worker's raw result straight to the
    channel via the toolbox CLI, bypassing claude -p entirely. Used only when
    narrate() has repeatedly failed or the report is too stale — the user
    should get SOMETHING rather than a silently dropped result."""
    channel = str(rep.get("channel"))
    slack = not channel.isdigit()
    script = os.path.join(ROOT, "slack_api.py" if slack else "discord_api.py")
    result = (rep.get("result") or "(empty)").strip()
    if len(result) > 1500:
        result = result[:1500] + "…"
    msg = (f"⚠️ worker {rep.get('name')} 已完成(exit {rep.get('exit')}),"
           f"但我没能正常汇报,先把原始结果直发给你:\n{result}")
    try:
        r = subprocess.run([sys.executable, script, "post", channel, msg],
                            cwd=ROOT, text=True, capture_output=True, timeout=60)
    except Exception as exc:
        print(f"[report] backstop post errored for '{rep.get('name')}': {exc}", flush=True)
        return False
    if r.returncode == 0:
        print(f"[report] backstop-delivered worker '{rep.get('name')}' to {channel}", flush=True)
        return True
    print(f"[report] backstop post failed for '{rep.get('name')}': "
          f"{(r.stderr or r.stdout)[:200]}", flush=True)
    return False


def main():
    limited = b.is_limited()
    if limited:
        print("[report] rate-limited — only backstop-eligible reports will be delivered", flush=True)
    for path in sorted(glob.glob(os.path.join(REPORTS_DIR, "*.json"))):
        try:
            with open(path) as f:
                rep = json.load(f)
        except Exception:
            os.remove(path)
            continue

        due_backstop = _backstop_due(rep)
        # Rate limit may have started or cleared mid-run (a prior narrate() in
        # this same pass can trip it) — re-check per report rather than once.
        limited = b.is_limited()

        if due_backstop:
            if backstop_post(rep):
                try:
                    os.remove(path)
                except OSError:
                    pass
            else:
                print(f"[report] backstop failed for '{rep.get('name')}' — will retry next tick", flush=True)
            continue

        if limited:
            print(f"[report] rate-limited — leaving '{rep.get('name')}' queued", flush=True)
            continue

        try:
            ok = narrate(rep)
        except Exception as exc:
            print(f"[report] error narrating {path}: {exc}", flush=True)
            ok = False

        if ok:
            # Delivered exactly once — narrate succeeded, no backstop needed.
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            rep["attempts"] = rep.get("attempts", 0) + 1
            rep["last_error"] = "narrate failed"
            try:
                with open(path, "w") as f:
                    json.dump(rep, f, ensure_ascii=False)
            except OSError:
                pass


if __name__ == "__main__":
    main()
