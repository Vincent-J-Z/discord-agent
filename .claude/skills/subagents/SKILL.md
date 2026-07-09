---
name: subagents
description: Dispatch real work to long-lived tmux workers and supervise them across invocations — the channel agent is the dispatcher, workers do the labor. Use for ANY substantial task (builds, installs, pipelines, coding tasks, long remote jobs), for steering a running worker (send follow-up instructions), and for checking on / collecting / reaping jobs a previous invocation started.
---

# Sub-agents (tmux-backed, persistent across @-invocations)

**You are the dispatcher, not the laborer.** The per-message run understands the
user, answers, dispatches, supervises, and reports — substantial work happens in
WORKERS: processes inside named **tmux** sessions that keep running after this
`claude -p` exits (each @ has a ~30-min cap and no memory beyond what it reads
back). A later invocation rediscovers a worker, reads its output, steers it, or
reaps it. State lives in `/workspace/subagents/<name>.json` (+ a logfile under
`/workspace/logs/`), so it survives container restarts.

One CLI does all of it: **`/app/src/subagent.py`** (tmux is preinstalled).

## Spawn

```bash
# A shell command — pass it as ONE quoted string. Options go BEFORE/AFTER it,
# never let them blend into the command.
python /app/src/subagent.py spawn <name> "<shell command>" \
    [--cwd DIR] [--channel <id>] [--note "what/why"] [--report]

# A claude WORKER (preferred for tasks): headless `claude -p` in tmux. It runs
# to completion, captures its session_id, and --report posts the RESULT text to
# the channel. Course-correct it afterward with `steer` (resume, below).
python /app/src/subagent.py claude <name> "<task brief>" \
    [--cwd DIR] [--channel <id>] [--note ...] [--model <m>] [--report]
```

Write the brief like a real handoff: goal, context (paths, prior findings),
constraints, definition of done. The worker reports back to YOU (the dispatcher),
NOT to the user — so it should produce a clear final result and leave progress
notes in its own output; do NOT tell it to post to the channel. With `--report`,
when it finishes the runtime wakes the dispatcher to narrate the outcome to the
user in the dispatcher's own voice (one assistant, one voice).

Interactive `claude` REPLs can't be workers in this deployment — they demand an
interactive `/login` (headless auth via env token isn't honored in the TUI).
Headless `claude -p` + `steer` is the supported pattern.

- `<name>` is a kebab-case task id: `video-backfill`, `beta-backtest`.
- `--channel <id>` records where to report; with **`--report`** a line is posted
  to that channel (exit code + last log lines) the moment the job finishes — so a
  long job tells the user itself without you blocking.
- `--cwd` e.g. `/workspace/beta` to run inside a cloned repo.

## Maintain (from THIS or a later invocation)

```bash
python /app/src/subagent.py list                  # all tracked jobs + running/done(exit N)
python /app/src/subagent.py logs  <name> [--lines 40]   # recent output (full history on disk)
python /app/src/subagent.py steer <name> "<follow-up>"  # claude worker: resume its session with a correction
python /app/src/subagent.py send  <name> "<text>" # shell/TUI job: type a line + Enter into the pane
python /app/src/subagent.py wait  <name> [--timeout 60] # block briefly until it exits
python /app/src/subagent.py kill  <name>          # stop it
python /app/src/subagent.py reap                  # forget state for sessions that ended
```

## Playbook

- **Any substantial task (≳2 min):** dispatch a worker with `--channel <here>
  --report`, tell the user what you started and how they'll hear back, and END
  your turn — don't sit and block, and don't do the labor inline.
- **Steering:** user wants a tweak / the worker drifted → `logs <name>` to see
  where it is, then for a claude worker `steer <name> "<adjustment>"` (resumes
  its session with your follow-up as a new message; the worker must have finished
  its current round — steer runs the next round). For a live shell/TUI job,
  `send <name> "<keys>"` types into its pane. `kill`/`reap` when truly done.
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
