"""Bridge Discord mentions to the Claude Code CLI.

When DISCORD_GUILD_ID is set, the bridge watches every text channel in that
guild that the bot can read, so it can be @-mentioned from anywhere and replies
go back to the channel the message came from. Without a guild id it falls back
to the single DISCORD_CHANNEL_ID (legacy behaviour).

In the container setup CLAUDE_CWD points at the mounted source tree (/app), so
the agent can edit its own code when a session asks it to; edits land on the
host bind mount.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote

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
# Per-channel Claude session ids. Each channel/thread becomes its own ongoing
# conversation that survives across invocations via `claude --resume`, so the
# bot keeps context for continuous dev work instead of starting cold each time.
# Transcripts live under CLAUDE_CONFIG_DIR (point it at /workspace to persist
# across container rebuilds). Send "/reset" in a channel to start fresh there.
SESSIONS_FILE = os.path.join(WORKSPACE, ".sessions.json")
SESSION_RESUME = os.environ.get("SESSION_RESUME", "1").strip().lower() not in ("0", "", "false", "no")
# Discord channel types we treat as text we can be summoned in.
TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12}
# Tier-1: reply chunking, attachments, usage tracking.
USAGE_FILE = os.path.join(WORKSPACE, ".usage.jsonl")
REPLY_CHUNK = int(os.environ.get("REPLY_CHUNK", "1900"))            # chars per posted msg
REPLY_FILE_THRESHOLD = int(os.environ.get("REPLY_FILE_THRESHOLD", "6000"))  # above → file
ATTACH_MAX_BYTES = int(os.environ.get("ATTACH_MAX_BYTES", str(25 * 1024 * 1024)))
START_TIME = time.time()
# Rate-limit gate: when Claude reports a usage/session limit, parse the reset
# time, pause until then, and queue requests so they're auto-answered on resume.
LIMITED_FILE = os.path.join(WORKSPACE, ".limited_until")
DEFERRED_DIR = os.path.join(WORKSPACE, ".deferred")
LIMIT_DEFAULT_COOLDOWN = int(os.environ.get("LIMIT_DEFAULT_COOLDOWN", "3600"))
HELP_TEXT = (
    "**Mochi_Bot** — @ 我即可。我能:\n"
    "• 读/写本服务器任意频道和 forum thread\n"
    "• 跑命令、读改自己的代码(热重载)、`ssh fin-agent`、查 DB/pipeline\n"
    "• 看你贴的图片/文件(截图报错也行)\n"
    "• 每个频道是一段持续会话(记得上下文);长任务会分阶段汇报\n"
    "• 每小时静默巡检,有需要才出声\n"
    "指令:`/help` · `/status` · `/reset`(重开本频道会话)"
)

allowed_raw = os.environ.get("BOT_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {x.strip() for x in allowed_raw.split(",") if x.strip()}

HEADERS = {"Authorization": f"Bot {TOKEN}"}
POST_HEADERS = {**HEADERS, "Content-Type": "application/json"}
WS_RE = re.compile(r"\s+")

# Channels we got 403 on — skip them on later ticks instead of retrying forever.
DENIED = set()

# Session map writes + per-channel serialization (so concurrent messages in the
# same channel don't resume the same session at once and clobber it).
_sessions_lock = threading.Lock()
_channel_locks = {}
_channel_locks_guard = threading.Lock()

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


def _read_sessions():
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def get_session(channel_id):
    return _read_sessions().get(str(channel_id))


def set_session(channel_id, session_id):
    with _sessions_lock:
        data = _read_sessions()
        data[str(channel_id)] = session_id
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, SESSIONS_FILE)


def clear_session(channel_id):
    with _sessions_lock:
        data = _read_sessions()
        if data.pop(str(channel_id), None) is not None:
            tmp = SESSIONS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, SESSIONS_FILE)


def channel_lock(channel_id):
    with _channel_locks_guard:
        lk = _channel_locks.get(str(channel_id))
        if lk is None:
            lk = threading.Lock()
            _channel_locks[str(channel_id)] = lk
        return lk


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


def _strip_leading_mention(content, user_id):
    """Drop a mention of user_id that the model already put at the start of its
    reply, so the bridge doesn't prepend a second one (double @)."""
    text = content.lstrip()
    toks = (f"<@{user_id}>", f"<@!{user_id}>")
    changed = True
    while changed:
        changed = False
        for tok in toks:
            if text.startswith(tok):
                text = text[len(tok):].lstrip()
                changed = True
    return text


def post(channel_id, content, mention_user_id=None):
    if mention_user_id:
        content = f"<@{mention_user_id}> {_strip_leading_mention(content, mention_user_id)}"
    # Build the allowed-mentions whitelist from the user ids ACTUALLY present in
    # the text (any <@id> / <@!id> the model wrote), plus the reply target. We
    # don't use {"parse": ["users"]} blanket because that would also let stray
    # ids in quoted/example text ping people; whitelisting only ids we see in
    # our own content keeps it deliberate while still letting Mochi @ others.
    ids = set(re.findall(r"<@!?(\d+)>", content))
    if mention_user_id:
        ids.add(str(mention_user_id))
    allowed_mentions = {"users": sorted(ids)} if ids else {"parse": []}
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


def react(channel_id, message_id, emoji="👀"):
    """Best-effort emoji ack so the sender knows the message was seen."""
    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            client.put(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/"
                f"{message_id}/reactions/{quote(emoji)}/@me"
            )
    except Exception:
        pass


def unreact(channel_id, message_id, emoji):
    """Best-effort removal of our own reaction (to swap the working ack for a
    done one once the reply is posted)."""
    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            client.delete(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/"
                f"{message_id}/reactions/{quote(emoji)}/@me"
            )
    except Exception:
        pass


def _typing_burst(channel_id, reply_len):
    """Fire the typing indicator ONCE, right before we post, so it shows up only
    while we're actually 'typing out' the answer — not for the whole long run
    (that's what the 👀 reaction + progress posts are for). One typing POST lasts
    ~10s in Discord and is cleared the moment the message lands, so we trigger it
    and pause briefly (scaled to reply length) to make the appearance natural."""
    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            client.post(f"https://discord.com/api/v10/channels/{channel_id}/typing")
    except Exception:
        pass
    # ~1s floor, +1s per 400 chars, capped at 4s so we never stall a reply long.
    time.sleep(min(4.0, max(1.0, reply_len / 400.0)))


def download_attachments(msg):
    """Download a message's attachments to disk so Claude can read them (vision
    for images, text/PDF for files). Returns local paths."""
    atts = msg.get("attachments") or []
    if not atts:
        return []
    base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "attachments", str(msg["id"]))
    os.makedirs(base, exist_ok=True)
    paths = []
    for a in atts:
        url, name = a.get("url"), (a.get("filename") or "file")
        if not url or (a.get("size") or 0) > ATTACH_MAX_BYTES:
            continue
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
            path = os.path.join(base, name)
            with open(path, "wb") as f:
                f.write(resp.content)
            paths.append(path)
        except Exception as exc:
            print(f"[bridge] attachment download failed ({name}): {exc}", flush=True)
    return paths


def _split_message(text, limit=None):
    """Split text into <=limit chunks on line boundaries; hard-split long lines."""
    limit = limit or REPLY_CHUNK
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def post_file(channel_id, content, filename, file_text, mention_user_id=None):
    """Post a short message with the full text attached as a file."""
    allowed = {"parse": []}
    if mention_user_id:
        allowed = {"users": [mention_user_id]}
        content = f"<@{mention_user_id}> {content}"
    data = {"payload_json": json.dumps({"content": content[:2000], "allowed_mentions": allowed})}
    files = {"files[0]": (filename, file_text.encode("utf-8"), "text/markdown")}
    with httpx.Client(timeout=30, headers=HEADERS) as client:  # auth only; httpx sets multipart
        resp = client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=data, files=files,
        )
        resp.raise_for_status()


def post_reply(channel_id, content, mention_user_id=None):
    """Post a reply, splitting long output across messages (or a file) instead of
    silently truncating at 2000 chars."""
    content = (content or "").strip() or "Claude returned no output."
    if len(content) <= 2000:
        post(channel_id, content, mention_user_id)
    elif len(content) <= REPLY_FILE_THRESHOLD:
        for i, chunk in enumerate(_split_message(content)):
            post(channel_id, chunk, mention_user_id if i == 0 else None)
    else:
        post_file(channel_id, "📄 回复较长,完整内容见附件:", "reply.md", content, mention_user_id)


def log_usage(channel_id, author, data):
    try:
        rec = {
            "ts": round(time.time()),
            "channel": str(channel_id),
            "author": author,
            "cost_usd": data.get("total_cost_usd"),
            "turns": data.get("num_turns"),
            "duration_ms": data.get("duration_ms"),
        }
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def build_status():
    up = int(time.time() - START_TIME)
    h, rem = divmod(up, 3600)
    m = rem // 60
    total, n = 0.0, 0
    try:
        with open(USAGE_FILE) as f:
            for line in f:
                try:
                    total += json.loads(line).get("cost_usd") or 0.0
                    n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    try:
        watched = len(list_text_channels())
    except Exception:
        watched = "?"
    limit_line = ""
    if is_limited():
        limit_line = f"\n• ⏳ 额度受限,约 {fmt_utc(limited_until())} 恢复(已排队 {len(list_deferred())} 条)"
    return (
        "**Status**\n"
        f"• model: `{CLAUDE_MODEL or 'default'}` · perms: `{PERMISSION_MODE}`\n"
        f"• session resume: {'on' if SESSION_RESUME else 'off'} · watched: {watched} channels\n"
        f"• uptime: {h}h{m}m · handled runs: {n} · 累计成本: ${total:.2f}"
        f"{limit_line}"
    )


class RateLimited(Exception):
    def __init__(self, reset_epoch):
        super().__init__("rate limited")
        self.reset_epoch = reset_epoch


def _looks_rate_limited(text):
    t = (text or "").lower()
    return any(k in t for k in ("session limit", "usage limit", "rate limit", "429"))


def parse_reset_epoch(text):
    """Parse 'resets 1:30am (UTC)' → epoch of the next such UTC time. If we can't
    parse it, fall back to now + LIMIT_DEFAULT_COOLDOWN so we still auto-retry."""
    now = datetime.datetime.now(datetime.timezone.utc)
    m = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", (text or "").lower())
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        if 0 <= hh < 24 and 0 <= mm < 60:
            target = now.replace(hour=hh, minute=mm, second=30, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            if (target - now).total_seconds() <= 25 * 3600:
                return target.timestamp()
    return now.timestamp() + LIMIT_DEFAULT_COOLDOWN


def set_limited(reset_epoch):
    try:
        with open(LIMITED_FILE, "w") as f:
            f.write(str(reset_epoch))
    except Exception:
        pass


def limited_until():
    try:
        with open(LIMITED_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def is_limited():
    return time.time() < limited_until()


def fmt_utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%H:%M UTC")


def defer_message(channel_id, msg, prompt):
    """Queue a request to be answered once the rate limit resets."""
    os.makedirs(DEFERRED_DIR, exist_ok=True)
    author = msg.get("author") or {}
    rec = {
        "channel_id": str(channel_id),
        "message_id": str(msg["id"]),
        "author_id": author.get("id"),
        "author": author.get("username"),
        "prompt": prompt,
    }
    dst = os.path.join(DEFERRED_DIR, f"{msg['id']}.json")
    tmp = dst + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, dst)


def list_deferred():
    try:
        return sorted(
            os.path.join(DEFERRED_DIR, n)
            for n in os.listdir(DEFERRED_DIR)
            if n.endswith(".json")
        )
    except FileNotFoundError:
        return []


def drain_deferred():
    """Re-answer queued requests after the limit resets, oldest first. Stops (and
    keeps the rest) if we get rate-limited again."""
    for path in list_deferred():
        if is_limited():
            return
        try:
            with open(path) as f:
                rec = json.load(f)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        ch = rec["channel_id"]
        try:
            with channel_lock(ch):
                reply = run_claude(rec.get("author") or "user", ch, rec.get("prompt") or "")
        except RateLimited:
            return  # still limited — leave this and the rest for next time
        except subprocess.TimeoutExpired:
            reply = f"⏱️ 这条排队任务超过 {TIMEOUT_SECONDS // 60} 分钟上限,被中断。"
        except Exception as exc:
            reply = f"Bridge error: {exc}"
        try:
            post_reply(ch, reply, mention_user_id=rec.get("author_id"))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def run_claude(author, channel_id, prompt, history=""):
    context_block = (
        f"Recent channel messages (oldest first, for context only):\n{history}\n\n"
        if history
        else ""
    )
    instruction = (
        "You are Mochi_Bot replying to a Discord message. Your full operating "
        "context, capabilities, and the /app/src/discord_api.py toolbox are described "
        "in CLAUDE.md (already loaded) — you are NOT limited to this channel: if "
        "asked about or to act in another channel/thread (e.g. an omega thread), "
        "fetch and act on it via the toolbox rather than saying you can't see it. "
        "Use the recent messages for context; act on the new Message; reply "
        "concisely in plain text. If this is more than a quick reply, DON'T go "
        "silent — post brief progress updates to this channel as you work "
        "(`python /app/src/discord_api.py post "
        f"{channel_id} \"...\"`), per CLAUDE.md; the bridge only posts your final "
        "answer.\n\n"
        f"Sender: {author}\nThis channel: {channel_id}\n\n{context_block}New Message:\n{prompt}"
    )
    base = [
        CLAUDE_BIN,
        "-p",
        "--permission-mode",
        PERMISSION_MODE,
        "--output-format",
        "json",
    ]
    if CLAUDE_MODEL:
        base.extend(["--model", CLAUDE_MODEL])

    def invoke(resume_id):
        cmd = list(base)
        if resume_id:
            cmd.extend(["--resume", resume_id])
        cmd.append(instruction)
        return subprocess.run(
            cmd,
            cwd=CLAUDE_CWD,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_SECONDS,
        )

    resume_id = get_session(channel_id) if SESSION_RESUME else None
    result = invoke(resume_id)
    # The stored session can go missing (e.g. config dir reset) — retry fresh.
    if result.returncode != 0 and resume_id:
        print(f"[bridge] resume failed for {channel_id}; starting a fresh session", flush=True)
        clear_session(channel_id)
        result = invoke(None)

    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        # If claude emitted a JSON error (e.g. rate limit), surface just its message.
        try:
            edata = json.loads(err)
            err = edata.get("result") or edata.get("error") or err
        except Exception:
            pass
        if _looks_rate_limited(err):
            reset = parse_reset_epoch(err)
            set_limited(reset)
            raise RateLimited(reset)
        return f"⚠️ {err[:MAX_RESPONSE_CHARS]}"

    try:
        data = json.loads(result.stdout or "{}")
    except Exception:
        return (result.stdout or "").strip() or "Claude returned no output."
    # A 0-exit result can still be a rate-limit error (is_error + 429).
    if data.get("is_error") and (data.get("api_error_status") == 429 or _looks_rate_limited(data.get("result"))):
        reset = parse_reset_epoch(data.get("result"))
        set_limited(reset)
        raise RateLimited(reset)
    if SESSION_RESUME and data.get("session_id"):
        set_session(channel_id, data["session_id"])
    log_usage(channel_id, author, data)
    # Full reply — post_reply() chunks/uploads it instead of truncating.
    return (data.get("result") or "").strip() or "Claude returned no output."


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
    react(channel_id, msg["id"], "👀")  # instant ack: "seen you"
    prompt = clean_prompt(content)
    low = prompt.strip().lower()
    # Built-in commands (no Claude run).
    if low in ("/reset", "/new", "reset session"):
        clear_session(channel_id)
        post(channel_id, "🔄 已重置本频道的会话,下一条消息从头开始。", mention_user_id=author_id)
        return
    if low in ("/help", "help"):
        post_reply(channel_id, HELP_TEXT, mention_user_id=author_id)
        return
    if low in ("/status", "status"):
        post_reply(channel_id, build_status(), mention_user_id=author_id)
        return
    # Pull in any attached images/files so Claude can read them.
    try:
        files = download_attachments(msg)
    except Exception as exc:
        print(f"[bridge] attachment handling failed: {exc}", flush=True)
        files = []
    if files:
        prompt = (prompt or "") + (
            "\n\n[The user attached files — view them with your Read tool: "
            + ", ".join(files) + "]"
        )
    if not prompt:
        prompt = "The user only mentioned you without extra text. Reply briefly and ask what they need."
    # Rate-limited right now → don't waste a call; queue it and auto-answer on reset.
    if is_limited():
        defer_message(channel_id, msg, prompt)
        react(channel_id, msg["id"], "⏳")
        post(channel_id, f"⏳ Claude 额度暂时用满,约 {fmt_utc(limited_until())} 恢复后我会自动回复你。",
             mention_user_id=author_id)
        return
    print(
        f"[bridge] handling {msg['id']} in {channel_id} "
        f"from {author.get('username', author_id)}",
        flush=True,
    )
    # Ack with a 👀 reaction so the sender immediately sees we picked it up — this
    # replaces the old "typing for the entire run" indicator. The live "working"
    # signal is now this reaction plus the progress posts Mochi makes itself; the
    # typing indicator is reserved for the moment we actually post the answer.
    react(channel_id, msg["id"], "👀")
    reply = "Bridge error."
    try:
        # Serialize same-channel messages so they continue one conversation in
        # order; different channels still run concurrently.
        with channel_lock(channel_id):
            try:
                history = fetch_recent(channel_id, msg["id"])
            except Exception as exc:
                print(f"[bridge] history fetch failed: {exc}", flush=True)
                history = ""
            try:
                reply = run_claude(author.get("username", author_id), channel_id, prompt, history)
            except RateLimited as rl:
                # Hit the limit mid-flight — queue it and tell the user it'll auto-resume.
                defer_message(channel_id, msg, prompt)
                react(channel_id, msg["id"], "⏳")
                reply = f"⏳ Claude 额度刚好用满,约 {fmt_utc(rl.reset_epoch)} 恢复后我会自动回复你。"
            except subprocess.TimeoutExpired:
                reply = (
                    f"⏱️ 这条任务超过了 {TIMEOUT_SECONDS // 60} 分钟的处理上限,被中断了。"
                    "可以把它拆小一点,或让我把长任务放后台跑、完成后回报。"
                )
            except Exception as exc:
                reply = f"Bridge error: {exc}"
    finally:
        # Now we have the answer — show "typing…" briefly so it looks like we're
        # writing it out, then post and swap the 👀 ack for a ✅ done mark.
        _typing_burst(channel_id, len(reply))
    post_reply(channel_id, reply, mention_user_id=author_id)
    unreact(channel_id, msg["id"], "👀")
    react(channel_id, msg["id"], "✅")


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
