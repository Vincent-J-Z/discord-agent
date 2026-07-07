#!/bin/sh
# Migrate this deployment — code AND state — to a remote host running Docker.
#
#   ./migrate-docker.sh <ssh-host> [remote-base]
#
# Copies:
#   - this repo (INCLUDING gitignored .env reference copy and CLAUDE.local.md)
#       -> <base>/app
#   - the whole workspace (operative .env with all tokens/keys, per-server
#     sessions, crossctx, subagents state, .ssh)   -> <base>/workspace
# then builds the image remotely and starts it via docker compose
# (restart: unless-stopped survives reboots).
#
# ⚠️ ONE bot token = ONE live instance. Replies will DOUBLE if both run.
# This script does NOT stop the local instance — after verifying the remote is
# healthy, stop the local one yourself:
#   container stop discord-agent          # Apple container (this Mac)
set -e

HOST="${1:?usage: migrate-docker.sh <ssh-host> [remote-base]}"
BASE="${2:-discord-agent-deploy}"   # relative to the remote $HOME unless absolute
SRC="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="${DISCORD_AGENT_WORKSPACE_HOST:-$HOME/discordAgentWorkspace}"

echo "==> preflight: docker on $HOST"
ssh "$HOST" 'docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1' \
  || { echo "ERROR: docker compose not available on $HOST — install Docker first"; exit 1; }

echo "==> creating $BASE on $HOST"
ssh "$HOST" "mkdir -p '$BASE/app' '$BASE/workspace'"

echo "==> syncing repo -> $HOST:$BASE/app (incl. .env + CLAUDE.local.md)"
rsync -az --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
  "$SRC/" "$HOST:$BASE/app/"

echo "==> syncing workspace ($(du -sh "$WORKSPACE" | cut -f1)) -> $HOST:$BASE/workspace"
rsync -az --delete --exclude 'tmp/' "$WORKSPACE/" "$HOST:$BASE/workspace/"

echo "==> remote: permissions (container runs as uid 10001), build & start"
ssh "$HOST" "set -e
  cd '$BASE'
  mkdir -p workspace/tmp
  sudo -n chown -R 10001 app workspace 2>/dev/null \
    || echo '   (could not chown to uid 10001 without sudo — if the container fails to write, run: sudo chown -R 10001 \$PWD/app \$PWD/workspace)'
  cd app
  DISCORD_AGENT_SOURCE_HOST=\"\$PWD\" \
  DISCORD_AGENT_WORKSPACE_HOST=\"\$PWD/../workspace\" \
  docker compose up -d --build
  sleep 8
  docker logs --tail 12 discord-agent"

cat <<'EOF'

==> done. Verify above that you see:
      [bridge] started …  /  [gateway] READY …  /  [slack] READY … (if configured)

⚠️ CUTOVER — the bot is now running TWICE. Once the remote looks healthy,
   stop the local instance (and its login autostart) on this machine:
      container stop discord-agent
      launchctl unload -w ~/Library/LaunchAgents/com.discord-agent.plist 2>/dev/null
EOF
