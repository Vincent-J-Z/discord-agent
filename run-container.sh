#!/bin/sh
# Launch the discord-agent container with Apple's `container` runtime.
#
# Two bind mounts:
#   /workspace -> runtime state + the operative .env (token, ids, config)
#   /app       -> this source tree, so the agent can edit its own code and the
#                 changes persist on the host (host is the source of truth)
#
# CLAUDE_CWD=/app (set in the workspace .env) makes the in-container Claude run
# in the mounted source, so "@bot change X in the code" edits land here.
#
# Rebuild the image first if the Containerfile or deps changed:
#   container build -t discord-agent:local -f Containerfile .
set -e

SRC="${DISCORD_AGENT_SOURCE_HOST:-$(cd "$(dirname "$0")" && pwd)}"
WORKSPACE="${DISCORD_AGENT_WORKSPACE_HOST:-$HOME/discordAgentWorkspace}"

# Disk-backed scratch (TMPDIR) so big video downloads/transcodes don't fill the
# RAM-backed /tmp tmpfs.
mkdir -p "$WORKSPACE/tmp"

container stop discord-agent 2>/dev/null || true
container rm discord-agent 2>/dev/null || container delete discord-agent 2>/dev/null || true

container run -d --name discord-agent \
  --cpus "${AGENT_CPUS:-6}" --memory "${AGENT_MEM:-6g}" \
  --user agent \
  --workdir /app \
  -e DISCORD_AGENT_WORKSPACE=/workspace \
  -e HOME=/home/agent \
  -e TMPDIR=/workspace/tmp \
  -v "$WORKSPACE":/workspace \
  -v "$SRC":/app \
  discord-agent:local

echo "started. logs: container logs -f discord-agent"
