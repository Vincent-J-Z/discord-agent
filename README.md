# discord-agent (Mochi_Bot)

A self-hosted Discord agent that bridges Discord mentions to the Claude Code CLI.
Mention the bot in any channel or forum thread and it answers — with real shell,
network, and tool access — running headless inside a hardened container.

## What it does
- **Real-time @-mention trigger** over the Discord Gateway (with a REST poller as
  fallback), dispatched to a worker pool so long tasks run concurrently.
- **Online presence** — holds a Gateway connection so the bot shows green.
- **Guild-wide** — reads/acts in any text channel and forum thread, replying in
  the channel it was summoned from.
- **Agentic** — runs as `claude -p` with full shell + network; can read/edit its
  own code (hot-reloads), use the `discord_api.py` toolbox, `ssh`, run pipelines,
  and install what it needs (`sudo apt`).

## Layout
| File | Role |
|------|------|
| `discord_agent_runtime.py` | Supervisor: launches + watches the bridge, gateway, status board; materializes `~/.ssh`. |
| `discord_claude_bridge.py` | REST poller + message handler (`claude -p`), hot-reload, dedup, history context. |
| `discord_gateway.py` | Gateway presence + real-time `@`-mention trigger → worker pool. |
| `discord_api.py` | Discord toolbox CLI (read/post/reply/react/edit/pin/threads/forum-post). |
| `discord_poll.py` / `discord_status.py` / `post_message.py` | One-shot poll, live status board, REST post helper. |
| `CLAUDE.md` | The bot's operating context, loaded by `claude` every run. |
| `Containerfile` / `compose.yaml` / `run-container.sh` / `autostart.sh` | Build, run, and boot-persist the container. |

## Setup
See [HANDOFF.md](HANDOFF.md) for full setup. In short:

```bash
cp .env.example ~/discordAgentWorkspace/.env      # fill in token, ids, OAuth token
cp secrets.env.example ~/discordAgentWorkspace/secrets.env   # optional task creds
./run-container.sh                                 # build is via Containerfile
```

## Configuration
Runtime config lives in the workspace `.env` (never committed). Key knobs:
`DISCORD_GUILD_ID` (watch the whole guild), `CLAUDE_MODEL`, `CLAUDE_PERMISSION_MODE`,
`CLAUDE_TIMEOUT_SECONDS`, `HISTORY_LIMIT`, `GATEWAY_WORKERS`, `BOT_ACTIVITY`.

> **Security:** the bot runs with `bypassPermissions` inside the container, so
> anyone who can mention it can make it run commands there. Restrict with
> `BOT_ALLOWED_USER_IDS` and keep secrets in the workspace (gitignored), not in
> the image.
