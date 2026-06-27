---
name: subagents
description: Run work that outlives a single @-invocation by spawning long-lived sub-agents in tmux, then maintaining them across invocations. Use for jobs longer than the ~30-min per-mention cap, anything you should background ("started, will report when done"), fanning a task out to parallel workers, or driving an interactive process — and for checking on / collecting / reaping jobs a previous invocation started.
---

# Sub-agents (tmux-backed, persistent across @-invocations)

Each @-mention is one `claude -p` with a ~30-min cap and no memory beyond what it
reads back. To do work that's **longer than that**, runs in the **background**,
**fans out**, or must be **driven over time**, spawn it as a **sub-agent**: a
process inside a named **tmux** session that keeps running after this `claude -p`
exits. A later invocation rediscovers it, reads its output, controls it, or
reaps it. State lives in `/workspace/subagents/<name>.json` (+ a logfile under
`/workspace/logs/`), so it survives container restarts.

One CLI does all of it: **`/app/src/subagent.py`** (tmux is preinstalled).

## Spawn

```bash
# A shell command — pass it as ONE quoted string. Options go BEFORE/AFTER it,
# never let them blend into the command.
python /app/src/subagent.py spawn <name> "<shell command>" \
    [--cwd DIR] [--channel <id>] [--note "what/why"] [--report]

# A sub-agent that is itself a headless claude -p (delegate a focused task):
python /app/src/subagent.py claude <name> "<prompt for the sub-agent>" \
    [--cwd DIR] [--channel <id>] [--note ...] [--model <m>] [--report]
```

- `<name>` is a kebab-case task id: `video-backfill`, `beta-backtest`.
- `--channel <id>` records where to report; with **`--report`** a line is posted
  to that channel (exit code + last log lines) the moment the job finishes — so a
  long job tells the user itself without you blocking.
- `--cwd` e.g. `/workspace/beta` to run inside a cloned repo.

## Maintain (from THIS or a later invocation)

```bash
python /app/src/subagent.py list                  # all tracked jobs + running/done(exit N)
python /app/src/subagent.py logs  <name> [--lines 40]   # recent output (full history on disk)
python /app/src/subagent.py send  <name> "<text>" # type a line + Enter into the session (drive it)
python /app/src/subagent.py wait  <name> [--timeout 60] # block briefly until it exits
python /app/src/subagent.py kill  <name>          # stop it
python /app/src/subagent.py reap                  # forget state for sessions that ended
```

## Playbook

- **Long job (> a few min):** spawn with `--channel <here> --report`, post
  "started, will report when done", and END your turn — don't sit and block.
  The job posts its own completion; a future @ can `logs`/`list` for detail.
- **Resuming:** at the start of a run, if the user asks "is it done?" / "how's X
  going?", run `list` then `logs <name>` and report — that's your only memory of
  what a prior invocation launched.
- **Fan-out:** spawn several `claude` sub-agents with distinct names + prompts,
  then `list`/`logs` to collect. Give each a narrow task and a `--note`.
- **Driving an interactive process:** `spawn` it, then `send` keystrokes (e.g.
  answer a prompt, issue a REPL command); `logs` to see the response.
- **Hygiene:** `reap` finished jobs so `list` stays meaningful. `kill` runaways.

## Notes / gotchas
- Pass the command as a **single quoted string**. For pipelines/loops, wrap in
  `bash -c '…'` *inside* that string.
- A sub-agent inherits this container's env (DB DSN, AWS, tokens) — same creds
  you have. It runs with the same `bypassPermissions`.
- `--report` needs `--channel`; without a channel, check results yourself later.
- Don't spawn two sub-agents with the same `<name>` (the second is refused until
  you `kill` the first).
- This is for *background/persistent* work. For a quick in-turn result, just run
  the command directly.
