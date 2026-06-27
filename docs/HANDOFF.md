# Discord Agent — Handoff / Setup

> A minimal Discord "agent" that wakes on a cron tick, polls one channel for new
> messages, processes them, and keeps a single live status-board message updated.
> No long-running process — each tick is one-shot.

---

## 1. Configure

Fill in `.env` (then `chmod 600 .env`):

- `DISCORD_BOT_TOKEN` — the bot token from the Discord Developer Portal.
- `DISCORD_CHANNEL_ID` — fallback / status-board channel. Used as the single
  channel when `DISCORD_GUILD_ID` is blank.
- `DISCORD_GUILD_ID` — set this so the bridge watches **every readable text
  channel** in the guild: the bot can be @-mentioned from any channel and its
  reply goes back to that channel. Per-channel cursors live in
  `.bridge_cursors.json`. Leave blank to stay single-channel.
- `DISCORD_BOT_ID` — this bot's own user id (so the poll skips its own messages).
- `DISCORD_ROLE_ID` — optional role the bot watches; leave blank to trigger only
  on direct bot mentions.
- `CLAUDE_CWD` — working dir for the in-container Claude. Set to `/app` (the
  mounted source tree) so the agent can read and edit its own code on request.
- `HISTORY_LIMIT` — recent messages fed to Claude as context per reply (default
  20; 0 disables).

The container is launched with Apple's `container` runtime via
[`run-container.sh`](run-container.sh), which bind-mounts the source tree at
`/app` (writable, host = source of truth) in addition to `/workspace`. The
`compose.yaml` mirrors the same mounts for Docker/Podman setups.
- `TICK_SECONDS` — optional poll interval; defaults to `60`.

For the container setup in this folder, put `.env`, `.status.md`, `.discord_seen`,
`.status_msg_id`, and `.speak_queue/` in the mounted workspace:

```text
$HOME/discordAgentWorkspace
```

The source code and container config stay in:

```text
<this repo directory>
```

The host workspace path can be changed without editing `compose.yaml`:

```bash
export DISCORD_AGENT_WORKSPACE_HOST=/absolute/path/to/discordAgentWorkspace
```

If bind-mount writes fail on Linux, set `HOST_UID` and `HOST_GID` before running
compose:

```bash
export HOST_UID=$(id -u)
export HOST_GID=$(id -g)
```

Enable Discord Developer Mode to copy IDs (right-click → Copy ID). The bot needs
read/send permissions in the channel; pinning the status board needs
`Manage Messages`.

## 2. Wake-up mechanism

There is no daemon. Schedule a once-a-minute cron tick that runs a prompt like
"poll Discord, then process new messages." In environments where a websocket
listener isn't available, the cron tick is the only thing that wakes the agent
while idle. Cron jobs scheduled in-session are session-only — re-create them
after a restart.

Each tick:
1. `python3 discord_poll.py` — print new messages since the saved cursor and
   advance it.
2. Handle anything addressed to the bot (`@ME` = direct mention or role mention);
   reply with `post_message.py`.
3. `python3 discord_status.py` — refresh the status board (edits one message in
   place; no channel spam).

## 3. Files

- `discord_poll.py` — one-shot poll. Persists a cursor in `.discord_seen`,
  filters out the bot's own messages and tool-noise, surfaces attachments, and
  flags `@ME`. Reads channel/bot/role ids from `.env`.
- `discord_status.py` — maintains ONE status message: posts it the first time,
  then edits in place each tick. Board text comes from `.status.md`; the message
  id is stored in `.status_msg_id`.
- `post_message.py` — post or reply via REST. `allowed_mentions` is restricted to
  an explicit user list, so stray `<@id>` text won't ping anyone by accident.
- `discord_claude_bridge.py` — bridge from Discord mentions to Claude Code.
  In the container setup, the image installs the Linux Claude Code package and
  authenticates with `CLAUDE_CODE_OAUTH_TOKEN` from the mounted workspace `.env`.
- `.env` — credentials and ids (not committed; `chmod 600`).
- `.status.md` — the status-board body.

The container image runs code from `/app` and mounts the workspace at
`/workspace`, so runtime state is kept out of the image.

## Container quick start

```bash
mkdir -p "$HOME/discordAgentWorkspace"
cp examples/.env.example "$HOME/discordAgentWorkspace/.env"
cp examples/status.example.md "$HOME/discordAgentWorkspace/.status.md"
printf '0\n' > "$HOME/discordAgentWorkspace/.discord_seen"
docker compose up -d --build
```

## Claude Code auth

Generate a long-lived OAuth token on a machine where Claude Code is already
installed and browser auth is available:

```bash
claude setup-token
```

Copy the printed token into the mounted workspace `.env`:

```env
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

For safety, also set `BOT_ALLOWED_USER_IDS` in the workspace `.env` to a comma
separated list of Discord user IDs before exposing the bridge in a shared
channel. Leave it empty only for a trusted private channel.

## 4. State files (reset to start fresh)

- `.discord_seen` — last-seen message id cursor. `0` = read from the beginning.
- `.status_msg_id` — id of the live status message. Empty = post a new one.

## 5. Conventions

- Reply to messages addressed to the bot explicitly — don't go silent after only
  doing background work.
- Keep a single hub for coordination rather than ad-hoc cross-talk.
- Treat any code the agent produces as a proposal: review before merging, never
  auto-merge.
