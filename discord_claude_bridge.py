"""Bridge Discord mentions to the Claude Code CLI.

When DISCORD_GUILD_ID is set, the bridge watches every text channel in that
guild that the bot can read, so it can be @-mentioned from anywhere and replies
go back to the channel the message came from. Without a guild id it falls back
to the single DISCORD_CHANNEL_ID (legacy behaviour).

In the container setup CLAUDE_CWD points at the mounted source tree (/app), so
the agent can edit its own code when a session asks it to; edits land on the
host bind mount.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import httpx
from dotenv import load_dotenv


ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get(
    "DISCORD_AGENT_WORKSPACE",
    os.path.expanduser("~/discordAgentWorkspace"),
)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
# Pipeline / task credentials (DSN, AWS, LLM keys, GH_TOKEN, …) live in a
# separate secrets file so they're not mixed with bot config. Loaded into the
# environment that the spawned `claude` (and its shell commands) inherit.
load_dotenv(os.path.join(WORKSPACE, "secrets.env"), override=True)

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
BOT_ID = os.environ["DISCORD_BOT_ID"]
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", ROOT)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
# Permission mode for the in-container claude. "dontAsk" blocks ALL shell
# execution (claude can only read/write files); that's why the bot couldn't
# post, run pipelines, etc. "bypassPermissions" lets it actually act — safe here
# because the container is the isolation boundary.
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "10"))
MAX_RESPONSE_CHARS = int(os.environ.get("MAX_RESPONSE_CHARS", "1800"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "20"))
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "240"))
# Per-channel "last seen message id" cursors, so each channel is tracked
# independently. Replaces the old single-channel .claude_bridge_seen file.
CURSORS_FILE = os.path.join(WORKSPACE, ".bridge_cursors.json")
# Atomic per-message claim dir, so the REST poller and the Gateway event path
# never both handle (and reply to) the same message.
HANDLED_DIR = os.path.join(WORKSPACE, ".handled")
# Discord channel types we treat as text we can be summoned in.
TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12}

allowed_raw = os.environ.get("BOT_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {x.strip() for x in allowed_raw.split(",") if x.strip()}

HEADERS = {"Authorization": f"Bot {TOKEN}"}
POST_HEADERS = {**HEADERS, "Content-Type": "application/json"}
WS_RE = re.compile(r"\s+")

# Channels we got 403 on — skip them on later ticks instead of retrying forever.
DENIED = set()

# Hot-reload: watch our own source + the .env files. When any changes, re-exec
# this process in place (same PID) so edits take effect without a container
# restart. The Claude side is already hot (a fresh `claude -p` per message reads
# files live); this covers the long-running bridge itself.
SELF = os.path.abspath(__file__)
WATCH_FILES = [
    SELF,
    os.path.join(ROOT, ".env"),
    os.path.join(WORKSPACE, ".env"),
    os.path.join(WORKSPACE, "secrets.env"),
]


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


WATCH_MTIMES = {p: _mtime(p) for p in WATCH_FILES}
_reload_warned = None


def maybe_reload():
    """If our source or .env changed, validate and re-exec in place."""
    global _reload_warned
    changed = [p for p in WATCH_FILES if _mtime(p) > WATCH_MTIMES.get(p, 0.0)]
    if not changed:
        return
    # Never swap in code that won't even import — a broken edit would crash the
    # bridge and (with no restart policy) take the whole container down.
    try:
        with open(SELF) as f:
            compile(f.read(), SELF, "exec")
    except SyntaxError as exc:
        sig = (_mtime(SELF), str(exc))
        if sig != _reload_warned:
            print(f"[bridge] reload skipped — syntax error: {exc}", flush=True)
            _reload_warned = sig
        return
    names = ", ".join(os.path.basename(p) for p in changed)
    print(f"[bridge] change in {names} — hot-reloading (execv)", flush=True)
    os.execv(sys.executable, [sys.executable, SELF])


def is_addressed(content):
    if f"<@{BOT_ID}>" in content or f"<@!{BOT_ID}>" in content:
        return True
    return bool(ROLE_ID and f"<@&{ROLE_ID}>" in content)


def clean_prompt(content):
    content = content.replace(f"<@{BOT_ID}>", "").replace(f"<@!{BOT_ID}>", "")
    if ROLE_ID:
        content = content.replace(f"<@&{ROLE_ID}>", "")
    return WS_RE.sub(" ", content).strip()


def load_cursors():
    if os.path.exists(CURSORS_FILE):
        try:
            with open(CURSORS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def save_cursors(cursors):
    os.makedirs(WORKSPACE, exist_ok=True)
    tmp = CURSORS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cursors, f)
    os.replace(tmp, CURSORS_FILE)


def list_active_threads():
    """Active thread ids in the guild (forum posts, threads under text channels).
    `GET /guilds/{id}/channels` does NOT return threads, so without this a @ in a
    forum channel like omega is never seen. A thread is just a channel for the
    messages/post endpoints, so its id slots straight into the watch list."""
    if not GUILD_ID:
        return []
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/guilds/{GUILD_ID}/threads/active",
        )
        response.raise_for_status()
        data = response.json()
    threads = data.get("threads", []) if isinstance(data, dict) else []
    return [
        str(t["id"])
        for t in threads
        if isinstance(t, dict) and t.get("id")
    ]


def list_text_channels():
    """Channel ids to watch this tick. With a guild id, enumerate every text
    channel plus every active thread (forum posts included); otherwise fall back
    to the single configured channel."""
    if not GUILD_ID:
        return [CHANNEL_ID] if CHANNEL_ID else []
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/guilds/{GUILD_ID}/channels",
        )
        response.raise_for_status()
        channels = response.json()
    ids = [
        str(c["id"])
        for c in channels
        if isinstance(c, dict) and c.get("type") in TEXT_CHANNEL_TYPES
    ]
    try:
        for tid in list_active_threads():
            if tid not in ids:
                ids.append(tid)
    except Exception as exc:
        print(f"[bridge] active-thread list error: {exc}", flush=True)
    if CHANNEL_ID and CHANNEL_ID not in ids:
        ids.append(CHANNEL_ID)
    return [c for c in ids if c not in DENIED]


def claim_message(message_id):
    """Atomically claim a message id. Returns True only for the first caller, so
    whichever of the poller / gateway gets there first handles it; the other
    skips. The gateway is near-instant, so it almost always wins."""
    os.makedirs(HANDLED_DIR, exist_ok=True)
    path = os.path.join(HANDLED_DIR, str(message_id))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def prune_handled(max_age=86400):
    try:
        now = time.time()
        for name in os.listdir(HANDLED_DIR):
            p = os.path.join(HANDLED_DIR, name)
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.remove(p)
            except OSError:
                pass
    except FileNotFoundError:
        pass


def fetch_messages(channel_id, after):
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": 50, "after": after},
        )
    if response.status_code == 403:
        DENIED.add(channel_id)
        return []
    response.raise_for_status()
    messages = response.json()
    if not isinstance(messages, list):
        return []
    return sorted(messages, key=lambda msg: int(msg["id"]))


def fetch_recent(channel_id, before_id):
    """Return the last HISTORY_LIMIT messages in `channel_id` before `before_id`,
    oldest-first, as plain "[author] text" lines for context. Same token/endpoint
    as fetch_messages — no extra permissions."""
    if HISTORY_LIMIT <= 0:
        return ""
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": HISTORY_LIMIT, "before": before_id},
        )
        response.raise_for_status()
        messages = response.json()
    if not isinstance(messages, list):
        return ""
    lines = []
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        name = (msg.get("author") or {}).get("username", "?")
        body = WS_RE.sub(" ", msg.get("content", "") or "").strip()
        if body:
            lines.append(f"[{name}] {body}")
    return "\n".join(lines)


def fetch_latest_id(channel_id):
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": 1},
        )
    if response.status_code == 403:
        DENIED.add(channel_id)
        return "0"
    response.raise_for_status()
    messages = response.json()
    if isinstance(messages, list) and messages:
        return messages[0]["id"]
    return "0"


def _typing_loop(channel_id, stop):
    """Keep the "Bot is typing…" indicator alive in `channel_id` until `stop`
    is set. Discord's typing state lasts ~10s, so we re-trigger every 8s. This
    is the only live signal users get while a long `claude -p` run is working."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/typing"
    while True:
        try:
            with httpx.Client(timeout=10, headers=HEADERS) as client:
                client.post(url)
        except Exception:
            pass
        if stop.wait(8):
            return


def post(channel_id, content, mention_user_id=None):
    allowed_mentions = {"parse": []}
    if mention_user_id:
        allowed_mentions = {"users": [mention_user_id]}
        content = f"<@{mention_user_id}> {content}"
    payload = {
        "content": content[:2000],
        "allowed_mentions": allowed_mentions,
    }
    with httpx.Client(timeout=20, headers=POST_HEADERS) as client:
        response = client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json=payload,
        )
        response.raise_for_status()


def run_claude(author, channel_id, prompt, history=""):
    context_block = (
        f"Recent channel messages (oldest first, for context only):\n{history}\n\n"
        if history
        else ""
    )
    instruction = (
        "You are Mochi_Bot replying to a Discord message. Your full operating "
        "context, capabilities, and the /app/discord_api.py toolbox are described "
        "in CLAUDE.md (already loaded) — you are NOT limited to this channel: if "
        "asked about or to act in another channel/thread (e.g. an omega thread), "
        "fetch and act on it via the toolbox rather than saying you can't see it. "
        "Use the recent messages for context; act on the new Message; reply "
        "concisely in plain text.\n\n"
        f"Sender: {author}\nThis channel: {channel_id}\n\n{context_block}New Message:\n{prompt}"
    )
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--permission-mode",
        PERMISSION_MODE,
        "--output-format",
        "text",
    ]
    if CLAUDE_MODEL:
        cmd.extend(["--model", CLAUDE_MODEL])
    cmd.append(instruction)
    result = subprocess.run(
        cmd,
        cwd=CLAUDE_CWD,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        return f"Claude Code failed: {err[:MAX_RESPONSE_CHARS]}"
    return (result.stdout or "").strip()[:MAX_RESPONSE_CHARS] or "Claude returned no output."


def handle_message(channel_id, msg):
    author = msg.get("author") or {}
    author_id = author.get("id", "")
    if author_id == BOT_ID:
        return
    if ALLOWED_USER_IDS and author_id not in ALLOWED_USER_IDS:
        return
    content = msg.get("content", "") or ""
    if not is_addressed(content):
        return
    if not claim_message(msg["id"]):
        return  # already handled by the other path (poller/gateway)
    prompt = clean_prompt(content)
    if not prompt:
        prompt = "The user only mentioned you without extra text. Reply briefly and ask what they need."
    print(
        f"[bridge] handling {msg['id']} in {channel_id} "
        f"from {author.get('username', author_id)}",
        flush=True,
    )
    # Show "Mochi_Bot is typing…" for the whole run so the channel can see it's
    # working rather than staring at silence for tens of seconds.
    stop_typing = threading.Event()
    typer = threading.Thread(
        target=_typing_loop, args=(channel_id, stop_typing), daemon=True
    )
    typer.start()
    try:
        try:
            history = fetch_recent(channel_id, msg["id"])
        except Exception as exc:
            print(f"[bridge] history fetch failed: {exc}", flush=True)
            history = ""
        try:
            reply = run_claude(author.get("username", author_id), channel_id, prompt, history)
        except subprocess.TimeoutExpired:
            reply = (
                f"⏱️ 这条任务超过了 {TIMEOUT_SECONDS // 60} 分钟的处理上限,被中断了。"
                "可以把它拆小一点,或让我把长任务放后台跑、完成后回报。"
            )
        except Exception as exc:
            reply = f"Bridge error: {exc}"
    finally:
        stop_typing.set()
    post(channel_id, reply, mention_user_id=author_id)


def main():
    mode = f"guild {GUILD_ID}" if GUILD_ID else f"channel {CHANNEL_ID}"
    print(f"[bridge] started ({mode})", flush=True)
    cursors = load_cursors()
    while True:
        maybe_reload()
        prune_handled()
        try:
            channels = list_text_channels()
        except Exception as exc:
            print(f"[bridge] channel list error: {exc}", flush=True)
            channels = [CHANNEL_ID] if CHANNEL_ID else []
        for channel_id in channels:
            try:
                if channel_id not in cursors:
                    # Newly seen channel: start at the latest id so we don't
                    # replay full history on first run.
                    cursors[channel_id] = fetch_latest_id(channel_id)
                    save_cursors(cursors)
                    continue
                messages = fetch_messages(channel_id, cursors[channel_id])
                for msg in messages:
                    cursors[channel_id] = msg["id"]
                    save_cursors(cursors)
                    handle_message(channel_id, msg)
            except Exception as exc:
                print(f"[bridge] error in {channel_id}: {exc}", flush=True)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
