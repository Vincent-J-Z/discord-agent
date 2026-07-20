"""Slack bridge — Socket Mode listener wired into the SAME agent brain.

One assistant, two mouths: this connects Slack to the same Claude runner the
Discord bridge uses — same sessions store (keys namespaced "slack:<channel>"),
same rate-limit gate, same run telemetry, and the shared cross-platform person
journal (crossctx), so a linked human talking to the bot on Slack and Discord
is talking to ONE continuous assistant.

Triggers: @-mentions in any channel the bot is in, and DMs (owner-linked
persons only — a DM is the privileged debug channel, same policy as Discord).

Socket Mode = outbound WebSocket, no public endpoint needed. Requires in the
workspace .env:
    SLACK_BOT_TOKEN=xoxb-…   (Web API: post/read/react)
    SLACK_APP_TOKEN=xapp-…   (Socket Mode connection)
and in the Slack app config: Socket Mode ON, Event Subscriptions with
app_mention + message.im, scopes: app_mentions:read chat:write channels:history
groups:history im:history im:read reactions:write users:read. The
assistant_view panel features below (viewing-context, suggested prompts,
auto-title) additionally want assistant_thread_started +
assistant_thread_context_changed subscribed and the assistant:write scope —
see the owner checklist wherever this bridge's operator tracks it.
"""
import asyncio
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time

import httpx
import websockets
from dotenv import load_dotenv

import discord_claude_bridge as b

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", os.path.expanduser("~/discordAgentWorkspace"))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
load_dotenv(os.path.join(WORKSPACE, "secrets.env"), override=True)

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "").strip()
API = "https://slack.com/api"
WORKERS = int(os.environ.get("SLACK_WORKERS", "2"))
HISTORY_LIMIT = int(os.environ.get("SLACK_HISTORY_LIMIT", "12"))
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)
# Rate-limit gate: a SEPARATE queue from Discord's DEFERRED_DIR (b.DEFERRED_DIR),
# since drain_deferred() over there posts via the Discord API and would mis-handle
# a Slack record. discord_agent_runtime watches this dir too and launches
# slack_resume.py to drain it once b.is_limited() clears (shared usage gate).
SLACK_DEFERRED_DIR = os.path.join(WORKSPACE, ".deferred_slack")
# Live thinking status: default OFF. When on, the setStatus indicator shown
# while a message is being handled tracks the model's real phase/thinking
# instead of the fixed three-line loading rotation. Same truthy convention as
# the other on/off switches in discord_claude_bridge.py.
SLACK_LIVE_THINKING = os.environ.get("SLACK_LIVE_THINKING", "0").strip().lower() not in ("0", "", "false", "no")
SLACK_THINKING_MIN_INTERVAL = 1.5  # seconds between pushes unless the phase changes

# Real streaming replies via Slack's chat.startStream/appendStream/stopStream
# (see https://docs.slack.dev/changelog/2025/10/7/chat-streaming/). Default OFF:
# with this off, handle() posts the full reply exactly as it does today — same
# truthy convention as SLACK_LIVE_THINKING above.
SLACK_STREAMING = os.environ.get("SLACK_STREAMING", "0").strip().lower() not in ("0", "", "false", "no")
SLACK_STREAM_MIN_INTERVAL = 1.0  # seconds between appendStream flushes
SLACK_STREAM_MIN_CHARS = 200  # or flush sooner once buffered text passes this

# Thread-as-session routing + a cross-thread shared memory board. Default OFF:
# with this off, every code path below that checks it is skipped and behavior
# is byte-for-byte the flat shared-session behavior that predates this flag.
# Same truthy convention as SLACK_LIVE_THINKING above.
SLACK_THREAD_SESSIONS = os.environ.get("SLACK_THREAD_SESSIONS", "0").strip().lower() not in ("0", "", "false", "no")
THREADMEM_DIR = os.path.join(WORKSPACE, "crossctx", "threadmem")
THREADMEM_MAX_BYTES = 4096  # tail-truncation budget for the injected board
_threadmem_lock = threading.Lock()

# Persons linked to a Discord owner id are owners here too (same human).
OWNER_PERSONS = {p for p in (b.person_for("discord", oid) for oid in b.OWNER_IDS) if p}
# Slack uids of those owners — so we can DM the operator a heads-up.
OWNER_SLACK_IDS = {uid for (plat, uid), name in b.USER_LINKS.items()
                   if plat == "slack" and name in OWNER_PERSONS}

SELF_ID = None
TEAM_ID = None  # this workspace's team id — chat.startStream wants recipient_team_id
_user_cache = {}
_channel_cache = {}
_seen = set()  # (channel, ts) — Socket Mode may redeliver

# assistant_view panel state — all process-local (in-memory), lost on restart.
# That's an accepted degradation (see docstrings below), never an error.
#
# Viewing-context: which channel the user had open in their main pane when they
# opened/switched the Assistant panel, from assistant_thread_started /
# assistant_thread_context_changed. Keyed by thread_ts (falls back to the
# assistant DM channel id for the rare event with no thread_ts yet).
_viewing_context = {}
# Thread keys (channel, thread_ts) already given an auto-title via
# assistant.threads.setTitle — so we only set it once per conversation.
_titled_threads = set()

# Suggested prompts pinned to the top of a NEW assistant panel via
# assistant.threads.setSuggestedPrompts (max 4, each {"title", "message"}).
# Plain constant so these are easy to retune without touching the call site.
SUGGESTED_PROMPTS = [
    {"title": "频道摘要", "message": "这个频道最近在聊什么?帮我梳理一下"},
    {"title": "查任务状态", "message": "现在有哪些在跑的任务?给我汇总一下进度"},
    {"title": "帮我查一个东西", "message": "帮我查一下:"},
    {"title": "你能做什么", "message": "你能帮我做哪些事?举几个例子"},
]


def api(method, **params):
    # Form-encoded, NOT json: Slack's read methods (users.info, conversations.*)
    # silently ignore a JSON body — that's how user/channel names never resolved.
    # Write methods accept form fine too (the classic Slack API encoding).
    r = httpx.post(f"{API}/{method}",
                   headers={"Authorization": f"Bearer {BOT_TOKEN}"},
                   data=params or {}, timeout=20)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"slack {method}: {d.get('error')}")
    return d


def user_name(uid):
    if not uid:
        return "?"
    if uid not in _user_cache:
        try:
            u = api("users.info", user=uid)["user"]
            _user_cache[uid] = (u.get("profile", {}).get("display_name")
                                or u.get("real_name") or u.get("name") or uid)
        except Exception:
            _user_cache[uid] = uid
    return _user_cache[uid]


def channel_name(cid):
    """Best-effort #name for a channel id, cached. Returns None (never raises)
    if the lookup fails — callers must fall back to the raw id."""
    if not cid:
        return None
    if cid not in _channel_cache:
        try:
            c = api("conversations.info", channel=cid)["channel"]
            _channel_cache[cid] = c.get("name") or c.get("user")  # DMs have no "name", just a user
        except Exception:
            _channel_cache[cid] = None
    return _channel_cache[cid]


def post(channel, text, thread_ts=None):
    """Deliver text COMPLETELY: chunked to Slack size, paced under Slack's
    ~1 msg/sec/channel limit, retrying rate-limits — a failed chunk never
    silently drops the rest (that's how replies got 'swallowed')."""
    text = (text or "").strip() or "(no output)"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    dropped = 0
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(1.2)  # pace multi-chunk sends under the per-channel limit
        kw = {"channel": channel, "text": chunk}
        if thread_ts:
            kw["thread_ts"] = thread_ts
        sent = False
        for attempt in range(4):
            try:
                api("chat.postMessage", **kw)
                sent = True
                break
            except Exception as exc:
                if "ratelimited" in str(exc) or "rate_limited" in str(exc):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                print(f"[slack] post failed in {channel}: {exc}", flush=True)
                break
        if not sent:
            dropped += 1
            print(f"[slack] DROPPED chunk {i + 1}/{len(chunks)} in {channel}", flush=True)
    if dropped:
        try:
            api("chat.postMessage", channel=channel,
                text=f"⚠️ 这条回复有 {dropped}/{len(chunks)} 段没发出去(发送失败,详见容器日志)。")
        except Exception:
            pass


# NOTE: Slack does NOT use reaction-based status marks (unlike the Discord
# bridge's 👀→✅ swap). Completion here is signalled by the reply itself plus
# clearing the thinking text — see handle(). The old react/unreact/set_status
# helpers were a Discord-ism whose only live effect was a stray ✅ appearing on
# messages answered by the rate-limit drain; removed so resume matches normal.


def set_thinking(channel, thread_ts, text="", loading=None):
    """Slack Agents 'thinking' indicator via assistant.threads.setStatus. `text`
    is a single status line; `loading` is a list Slack rotates through. Empty
    text clears it (sending a reply also clears it, 2-min timeout otherwise).
    Best-effort — needs the Agents feature enabled to actually render in the UI.

    IMPORTANT: `loading_messages` is sticky on Slack's side — once a call sets
    it, later calls that omit the param do NOT clear it, so the rotation keeps
    playing over a plain `status` update. Any caller that needs its `text` to
    actually win over a previously-set rotation must pass `loading` too (a
    single-element list pins the indicator to that one message, i.e. no visible
    rotation, which is exactly a static status). Only omit `loading` when you
    know no rotation was ever primed for this thread_ts."""
    try:
        # An empty status string CLEARS the indicator (per Slack docs), so when
        # we mean "show thinking" the status must be non-empty even if loading
        # messages are supplied — otherwise nothing renders at all.
        if loading and not text:
            text = loading[0]
        kw = {"channel_id": channel, "thread_ts": thread_ts, "status": text}
        if loading:
            kw["loading_messages"] = json.dumps(loading, ensure_ascii=False)
        api("assistant.threads.setStatus", **kw)
    except Exception as exc:
        print(f"[slack] setStatus: {exc}", flush=True)


def set_suggested_prompts(channel, thread_ts):
    """Pin SUGGESTED_PROMPTS to the top of a freshly-opened Assistant panel via
    assistant.threads.setSuggestedPrompts. Called once per assistant_thread_started
    event. Best-effort — needs the Agents & Assistants feature (assistant:write
    scope) actually enabled, same caveat as set_thinking()."""
    try:
        api("assistant.threads.setSuggestedPrompts", channel_id=channel, thread_ts=thread_ts,
            prompts=json.dumps(SUGGESTED_PROMPTS, ensure_ascii=False))
    except Exception as exc:
        print(f"[slack] setSuggestedPrompts: {exc}", flush=True)


def _lookup_viewing_channel(channel, thread_ts):
    """The channel the user currently has open in their main pane, as last
    reported for this assistant thread — or None if we never saw one (process
    just restarted, or the panel events aren't subscribed). Missing is a
    normal, silent case: callers must degrade to no-context, never error."""
    return _viewing_context.get(thread_ts) or _viewing_context.get(channel)


def _maybe_set_thread_title(channel, thread_ts, text):
    """Auto-title a NEW assistant thread from the user's first message (first
    ~40 chars, no extra model call). No-ops on every later message in the same
    thread — _titled_threads tracks what's already been set, process-local so
    a restart just means the title gets (harmlessly) re-set once more."""
    if not thread_ts:
        return
    key = (channel, thread_ts)
    if key in _titled_threads:
        return
    _titled_threads.add(key)
    title = " ".join((text or "").split())[:40].strip()
    if not title:
        return
    try:
        api("assistant.threads.setTitle", channel_id=channel, thread_ts=thread_ts, title=title)
    except Exception as exc:
        print(f"[slack] setTitle: {exc}", flush=True)


def on_assistant_thread_event(ev):
    """Handle assistant_thread_started / assistant_thread_context_changed:
    record the user's currently-viewed channel (assistant_thread.context.channel_id)
    against this assistant thread, and on the 'started' event also pin the
    suggested prompts. Both events share the same assistant_thread payload
    shape; only the trigger differs."""
    at = ev.get("assistant_thread") or {}
    channel = at.get("channel_id")
    thread_ts = at.get("thread_ts")
    ctx_channel = (at.get("context") or {}).get("channel_id")
    key = thread_ts or channel
    if key and ctx_channel:
        _viewing_context[key] = ctx_channel
        print(f"[slack] viewing-context: thread {key} -> channel {ctx_channel}", flush=True)
    if ev.get("type") == "assistant_thread_started" and channel and thread_ts:
        set_suggested_prompts(channel, thread_ts)


def _progress_text(phase, thinking):
    """One status line for the live-thinking indicator: the emoji from `phase`
    (💭/🔧/✍️) plus the last line of `thinking`, truncated. Falls back to the
    bare phase when there's no thinking text yet."""
    phase = (phase or "").strip()
    emoji = phase.split(" ", 1)[0] if phase else ""
    snippet = (thinking or "").strip()
    if snippet:
        snippet = snippet.splitlines()[-1].strip()
    text = f"{emoji} {snippet}".strip() if snippet else phase
    return text[:110]


def _make_progress_cb(channel, ts):
    """Build an on_progress(phase, thinking) callback for run_claude() that
    pushes throttled live-status updates via set_thinking(). Pushes immediately
    on a phase change, otherwise at most once per SLACK_THINKING_MIN_INTERVAL —
    keeps this well under Slack's rate limits during a long tool-heavy run."""
    state = {"last_push": 0.0, "last_phase": None}

    def _cb(phase, thinking):
        now = time.monotonic()
        if phase == state["last_phase"] and now - state["last_push"] < SLACK_THINKING_MIN_INTERVAL:
            return
        state["last_push"] = now
        state["last_phase"] = phase
        text = _progress_text(phase, thinking)
        # Pass a single-element `loading` too: the initial call in handle() may
        # have primed a rotation (if SLACK_LIVE_THINKING was toggled mid-thread
        # or a prior indicator lingered), and a plain `text`-only update would
        # not clear it — see set_thinking()'s docstring. A one-item loading
        # list pins the indicator to this exact text with no visible rotation.
        set_thinking(channel, ts, text, loading=[text])

    return _cb


class _Streamer:
    """Buffers on_text() answer-text fragments from run_claude() and flushes
    them to Slack's chat.startStream/appendStream/stopStream, throttled to
    roughly one call per SLACK_STREAM_MIN_INTERVAL (well under the Tier-4
    100/min limit on appendStream). `markdown_text` on start/append/stop is
    APPENDED to the message, not an overwrite — see the Oct 2025 streaming
    changelog — so callers must only ever pass the new incremental fragment,
    never the accumulated text.

    Any Slack API error marks the stream `broken`; from that point on() is a
    no-op and the caller (handle()) must fall back to post()-ing the full
    reply so the user still gets a complete answer. `on_clear`, if given, is
    invoked once on the first fragment (used to clear the live-thinking
    indicator once the real answer starts appearing)."""

    def __init__(self, channel, thread_ts, recipient_user_id, on_clear=None):
        self.channel = channel
        self.thread_ts = thread_ts
        self.recipient_user_id = recipient_user_id
        self.on_clear = on_clear
        self.ts = None
        self.buf = ""
        self.broken = False
        self.last_flush = 0.0
        self.lock = threading.Lock()

    def on_text(self, delta):
        if self.broken or not delta:
            return
        if self.on_clear is not None:
            cb, self.on_clear = self.on_clear, None
            try:
                cb()
            except Exception:
                pass
        with self.lock:
            self.buf += delta
            now = time.monotonic()
            due = (now - self.last_flush >= SLACK_STREAM_MIN_INTERVAL
                   or len(self.buf) >= SLACK_STREAM_MIN_CHARS)
            if not due:
                return
            text, self.buf = self.buf, ""
            self.last_flush = now
        self._send(text)

    def _send(self, text):
        try:
            if self.ts is None:
                kw = {"channel": self.channel, "markdown_text": text}
                if self.thread_ts:
                    kw["thread_ts"] = self.thread_ts
                if TEAM_ID:
                    kw["recipient_team_id"] = TEAM_ID
                if self.recipient_user_id:
                    kw["recipient_user_id"] = self.recipient_user_id
                d = api("chat.startStream", **kw)
                self.ts = d.get("ts")
            elif text:
                api("chat.appendStream", channel=self.channel, ts=self.ts, markdown_text=text)
        except Exception as exc:
            print(f"[slack] stream error in {self.channel}: {exc}", flush=True)
            self.broken = True

    def stop(self):
        """Flush any remaining buffer and close the stream. Returns True if the
        stream is now the system of record for the reply (caller must NOT also
        post() the full text); False if it never started or broke along the
        way — the caller must post() the full reply as usual."""
        if self.broken or self.ts is None:
            # Broken mid-stream, or never got a single fragment (empty/instant
            # error reply) — nothing coherent to stop(); caller falls back.
            if self.broken and self.ts is not None:
                try:
                    api("chat.stopStream", channel=self.channel, ts=self.ts)
                except Exception:
                    pass
            return False
        with self.lock:
            text, self.buf = self.buf, ""
        try:
            kw = {"channel": self.channel, "ts": self.ts}
            if text:
                kw["markdown_text"] = text
            api("chat.stopStream", **kw)
            return True
        except Exception as exc:
            print(f"[slack] stopStream error in {self.channel}: {exc}", flush=True)
            return False


def download_images(ev):
    """Download image attachments of a Slack message to the slack workdir and
    return their local absolute paths, so the agent can Read them (Claude is
    multimodal). Non-image files are skipped. Requires the files:read scope."""
    files = ev.get("files") or []
    if not files:
        return []
    media_dir = os.path.join(b.ensure_server_dir("_slack"), "tmp", "slack_media")
    os.makedirs(media_dir, exist_ok=True)
    paths = []
    for f in files:
        mt = f.get("mimetype") or ""
        url = f.get("url_private_download") or f.get("url_private")
        if not mt.startswith("image/") or not url:
            continue
        ext = (f.get("filetype") or "png").split("?")[0][:5] or "png"
        dest = os.path.join(media_dir, f"{f.get('id', 'img')}.{ext}")
        try:
            r = httpx.get(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"},
                          timeout=30, follow_redirects=True)
            # a token/scope failure returns an HTML login page, not the image
            if r.status_code == 200 and not r.content[:15].lstrip().lower().startswith(b"<!doctype"):
                with open(dest, "wb") as fh:
                    fh.write(r.content)
                paths.append(dest)
            else:
                print(f"[slack] image fetch not an image (status {r.status_code})", flush=True)
        except Exception as exc:
            print(f"[slack] image download failed: {exc}", flush=True)
    return paths


def fetch_history(channel, before_ts):
    """Recent messages (oldest first) for context, excluding the trigger itself."""
    try:
        d = api("conversations.history", channel=channel, limit=HISTORY_LIMIT + 1)
    except Exception as exc:
        print(f"[slack] history fetch failed: {exc}", flush=True)
        return ""
    lines = []
    for m in sorted(d.get("messages", []), key=lambda x: float(x.get("ts", 0))):
        st = m.get("subtype")
        if m.get("ts") == before_ts or (st and st != "file_share"):
            continue
        body = " ".join((m.get("text") or "").split())
        if m.get("files"):
            imgs = sum(1 for f in m["files"] if (f.get("mimetype") or "").startswith("image/"))
            if imgs:
                body = (body + f" [附{imgs}张图片]").strip()
        if not body:
            continue
        lines.append(f"[{user_name(m.get('user') or m.get('bot_id'))}] {body[:500]}")
    return "\n".join(lines[-HISTORY_LIMIT:])


def fetch_thread_history(channel, thread_ts, before_ts):
    """Recent messages (oldest first) within ONE thread, for context — used in
    place of fetch_history() when SLACK_THREAD_SESSIONS routes a message into
    its own thread-session, so context stays scoped to that thread instead of
    the whole channel. Same shape/limits as fetch_history()."""
    try:
        d = api("conversations.replies", channel=channel, ts=thread_ts, limit=HISTORY_LIMIT + 1)
    except Exception as exc:
        print(f"[slack] thread history fetch failed: {exc}", flush=True)
        return ""
    lines = []
    for m in sorted(d.get("messages", []), key=lambda x: float(x.get("ts", 0))):
        st = m.get("subtype")
        if m.get("ts") == before_ts or (st and st != "file_share"):
            continue
        body = " ".join((m.get("text") or "").split())
        if m.get("files"):
            imgs = sum(1 for f in m["files"] if (f.get("mimetype") or "").startswith("image/"))
            if imgs:
                body = (body + f" [附{imgs}张图片]").strip()
        if not body:
            continue
        lines.append(f"[{user_name(m.get('user') or m.get('bot_id'))}] {body[:500]}")
    return "\n".join(lines[-HISTORY_LIMIT:])


def _route_session(channel, ts, thread_ts, is_dm):
    """SLACK_THREAD_SESSIONS routing (only called from behind that flag).

    DM: always thread-ified — a top-level message roots a NEW thread/session
    (session_thread = its own ts); replying inside an existing thread continues
    that same session. Channel: an explicit thread_ts gets its own session;
    top-level stays the shared flat session (session_thread=None), same as the
    pre-flag behavior for that case.

    Returns (session_thread, reply_thread_ts, key). reply_thread_ts is where
    the reply is posted; key (also used as the channel_lock key) is the
    "slack:{channel}" flat key when session_thread is falsy, else
    "slack:{channel}:{session_thread}" so distinct threads run/serialize
    independently of each other and of the flat channel session."""
    if is_dm:
        session_thread = thread_ts or ts
    else:
        session_thread = thread_ts or None
    reply_thread_ts = session_thread
    key = f"slack:{channel}:{session_thread}" if session_thread else f"slack:{channel}"
    return session_thread, reply_thread_ts, key


def _threadmem_path(channel):
    return os.path.join(THREADMEM_DIR, f"{channel}.md")


def _threadmem_read_block(channel):
    """Tail-truncated shared memory board for this channel/DM, formatted for
    injection into the instruction — visible to every thread-session in this
    channel so knowledge doesn't get siloed inside one thread. Missing/empty
    file -> "" (no injection, never an error)."""
    try:
        with open(_threadmem_path(channel), "rb") as f:
            data = f.read()
    except OSError:
        return ""
    if len(data) > THREADMEM_MAX_BYTES:
        data = data[-THREADMEM_MAX_BYTES:]
    content = data.decode("utf-8", errors="ignore").strip()
    if not content:
        return ""
    return ("=== 跨-thread 共享记忆板(本会话所有对话窗口的公共记忆,可引用) ===\n"
            + content + "\n\n")


_THREADMEM_LAST_RE = re.compile(r"\(last: [^)]*\)\s*$")


def _threadmem_upsert(channel, session_thread, first_line):
    """Mechanically record/refresh one directory line per thread-session — NO
    model call. If this session_thread already has a line, only its `last`
    timestamp is refreshed (the original snippet is kept, so repeated calls on
    the same thread are idempotent — no duplicate lines). Otherwise a new line
    is appended. This file doubles as a directory of what's being discussed
    where. Best-effort: any failure is swallowed, never raised into handle()."""
    try:
        label = session_thread or "main"
        now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        snippet = " ".join((first_line or "").split())[:60]
        prefix = f"- [{label}] "
        new_line = f"{prefix}{snippet} (last: {now})"
        path = _threadmem_path(channel)
        with _threadmem_lock:
            os.makedirs(THREADMEM_DIR, exist_ok=True)
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except FileNotFoundError:
                lines = []
            found = False
            for i, ln in enumerate(lines):
                if ln.startswith(prefix):
                    lines[i] = _THREADMEM_LAST_RE.sub(f"(last: {now})", ln.rstrip())
                    found = True
                    break
            if not found:
                lines.append(new_line)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
    except Exception as exc:
        print(f"[slack] threadmem upsert failed: {exc}", flush=True)


def build_instruction(author, channel, prompt, history, crossctx, is_dm, owner_dm, images=None,
                       threadmem_content="", threadmem_path=None, reply_thread_ts=None,
                       viewing_channel=None):
    progress_post_cmd = f"python /app/src/slack_api.py post {channel} \"...\""
    worker_dispatch_hint = f"subagent.py claude <name> \"<brief>\" --channel <this channel> --report"
    thread_note = ""
    if reply_thread_ts:
        progress_post_cmd += f" --thread {reply_thread_ts}"
        worker_dispatch_hint += f" --thread {reply_thread_ts}"
        thread_note = (f" (operating inside thread {reply_thread_ts} — any reply, progress "
                       "post, or worker you dispatch must stay in this thread)")
    context_block = (
        f"Recent channel messages (oldest first, for context only):\n{history}\n\n"
        if history else ""
    )
    where = "a private Slack DM from your operator (the bot's owner)" if owner_dm \
        else ("a private Slack DM" if is_dm else "a Slack channel message")
    lines = [
        f"You are {b.AGENT_NAME} replying to {where}. This conversation is on "
        "SLACK, not Discord.",
        "For anything Slack-side use the SLACK toolbox — "
        "`python /app/src/slack_api.py channels|read|post|react` (channel ids like "
        "C…/D…, threads via --thread <ts>). Slack formatting: *bold*, _italic_, "
        "`code` — no Discord markdown.",
    ]
    if owner_dm:
        lines.append(
            "This DM is your privileged debug/control channel: you have FULL "
            "cross-platform access. You may inspect and act across ALL Discord "
            "servers the bot is in (use `python /app/src/discord_api.py` — whoami/"
            "channels/threads/read work across every joined server here) as well "
            "as Slack, look at internals and configuration, and answer the owner "
            "candidly."
        )
    else:
        lines.append(
            "Stay within Slack for this conversation. Do NOT discuss or reveal "
            "the bot's internals — infrastructure, other deployments or platforms, "
            "directory layout, or absolute paths. If asked, briefly decline as "
            "something you don't do, and move on."
        )
    if viewing_channel:
        label = channel_name(viewing_channel)
        label = f"#{label}" if label else viewing_channel
        lines.append(
            f"用户当前在 Slack 主界面里正看着频道 {label}(channel_id={viewing_channel})—— "
            "这是 Slack 的 Assistant 面板上下文,仅供你判断他这句话可能指的是哪儿,不要"
            "假设他一定是在问这个频道,该频道的具体消息内容你并没有(除非另有提供)。"
        )
    lines.append(
        "Use the recent messages for context; act on the new Message. Your final "
        "answer IS delivered to this channel in full — long answers are split into "
        "several messages automatically, so put the COMPLETE deliverable in it: the "
        "actual results, numbers, findings, and any decision you need from the "
        "user. NEVER leave the substance only in local files or session memory — "
        "the user cannot see your machine, so 'full log in tmp/x.txt' delivers "
        "nothing; quote the relevant content itself. For longer work, post brief "
        f"progress updates as you go (`{progress_post_cmd}`) so nothing stays "
        "invisible. You are the dispatcher, not the laborer: substantial work "
        "(builds, installs, pipelines, long jobs, real coding tasks) must be "
        f"handed to a tmux worker — {worker_dispatch_hint} (see CLAUDE.md) — then "
        "supervised across turns with list/logs and course-corrected with `steer "
        "<name> \"...\"`; don't grind it inside this reply run. FIRST on every "
        "message run `python /app/src/subagent.py list --channel <this channel>` "
        "to see tasks already going here and route any follow-up to the right "
        "one (ask if ambiguous). Do NOT put a status/done "
        "checkmark (✅) in your reply text — the reply itself is the completion "
        "signal on Slack. Keep the prose free of status emoji."
    )
    if threadmem_path:
        lines.append(
            "This channel/DM has a cross-thread shared memory board — a plain "
            "markdown file every thread-session here can read (injected above/below "
            "as context when non-empty). If you learn something worth remembering "
            f"across conversation windows (a durable fact, decision, or conclusion), "
            f"you may append it yourself to {threadmem_path} so other threads can "
            "pick it up too — not required every turn, only when it's worth keeping."
        )
    img_block = ""
    if images:
        img_block = (
            f"\n\n[发信人上传了 {len(images)} 张图片,已下载到本地。用 Read 工具"
            "查看下列绝对路径的图片,把图片内容纳入你的回答:\n"
            + "\n".join(f"- {p}" for p in images) + "\n]"
        )
    return ("\n".join(lines)
            + f"\n\nSender: {author}\nThis Slack channel: {channel}{thread_note}\n\n"
            + f"{threadmem_content}{crossctx}{context_block}New Message:\n{prompt}{img_block}")


def _slack_defer_path(channel, ts):
    return os.path.join(SLACK_DEFERRED_DIR, f"{channel}__{str(ts).replace('.', '_')}.json")


def defer_slack_message(channel, ts, thread_ts, user, author, person, text, is_dm, owner_dm, images):
    """Queue a rate-limited Slack event to be answered once the limit resets."""
    os.makedirs(SLACK_DEFERRED_DIR, exist_ok=True)
    rec = {
        "channel": channel,
        "ts": ts,
        "thread_ts": thread_ts,
        "user": user,
        "author": author,
        "person": person,
        "text": text,
        "is_dm": is_dm,
        "owner_dm": owner_dm,
        "images": images or [],
    }
    dst = _slack_defer_path(channel, ts)
    tmp = dst + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, dst)


def list_deferred_slack():
    try:
        return sorted(
            os.path.join(SLACK_DEFERRED_DIR, n)
            for n in os.listdir(SLACK_DEFERRED_DIR)
            if n.endswith(".json")
        )
    except FileNotFoundError:
        return []


def drain_deferred_slack():
    """Mirror of b.drain_deferred() for Slack: one coherent catch-up per
    channel/thread — read the backlog and respond to the current state instead
    of replaying every stale message. Stops (leaving the rest queued) if we get
    rate-limited again, same philosophy as the Discord drain.

    When SLACK_THREAD_SESSIONS is on, grouping/session-key/reply-target use the
    SAME _route_session() rule handle() uses (recomputed from each record's
    stored channel/ts/thread_ts/is_dm) instead of the flat (channel, thread_ts)
    grouping — so this never replays into the wrong session. Off, the original
    (channel, thread_ts) grouping and flat key are unchanged."""
    groups = {}
    for path in list_deferred_slack():
        try:
            with open(path) as f:
                rec = json.load(f)
            rec["_path"] = path
            if SLACK_THREAD_SESSIONS:
                _, _, gkey = _route_session(rec.get("channel"), rec.get("ts"),
                                             rec.get("thread_ts"), rec.get("is_dm"))
            else:
                gkey = (rec.get("channel"), rec.get("thread_ts"))
            groups.setdefault(gkey, []).append(rec)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
    for recs in groups.values():
        if b.is_limited():
            return  # still limited — leave the rest for next time
        recs.sort(key=lambda r: float(r.get("ts") or 0))
        latest = recs[-1]
        channel = latest.get("channel")
        author = latest.get("author") or "user"
        person = latest.get("person")
        is_dm = latest.get("is_dm")
        owner_dm = latest.get("owner_dm")
        images = latest.get("images") or []
        text = latest.get("text") or ""
        catchup_text = (
            "[You were rate-limited and just came back online. The recent channel "
            "activity is in your context — catch up on what happened while you were "
            "out and respond to what currently needs you, one item at a time. Do "
            "NOT reply to each old message separately.]\n\n" + text
        )
        if SLACK_THREAD_SESSIONS:
            session_thread, reply_thread_ts, key = _route_session(
                channel, latest.get("ts"), latest.get("thread_ts"), is_dm)
        else:
            session_thread, reply_thread_ts, key = None, latest.get("thread_ts"), f"slack:{channel}"
        try:
            if SLACK_THREAD_SESSIONS and session_thread:
                history = fetch_thread_history(channel, session_thread, latest.get("ts"))
            else:
                history = fetch_history(channel, latest.get("ts"))
        except Exception:
            history = ""
        crossctx = b.crossctx_block(person, "slack", channel)
        threadmem_content = _threadmem_read_block(channel) if SLACK_THREAD_SESSIONS else ""
        threadmem_path = _threadmem_path(channel) if SLACK_THREAD_SESSIONS else None
        viewing_channel = _lookup_viewing_channel(channel, latest.get("thread_ts"))
        instruction = build_instruction(author, channel, catchup_text, history, crossctx,
                                         is_dm, owner_dm, images,
                                         threadmem_content=threadmem_content,
                                         threadmem_path=threadmem_path,
                                         reply_thread_ts=reply_thread_ts,
                                         viewing_channel=viewing_channel)
        try:
            with b.channel_lock(key):
                reply = b.run_claude(author, key, catchup_text, guild_id="_slack",
                                     is_dm=is_dm, owner_dm=owner_dm, instruction=instruction)
        except b.RateLimited:
            return  # hit it again mid-drain — leave this and later groups queued
        except subprocess.TimeoutExpired:
            reply = f"⏱️ 排队的任务超过 {b.TIMEOUT_SECONDS // 60} 分钟上限,被中断。"
        except Exception as exc:
            reply = f"⚠️ bridge error: {str(exc)[:300]}"
        try:
            post(channel, reply, reply_thread_ts)
            b.log_crossctx(person, "slack", channel, text, reply)
            if SLACK_THREAD_SESSIONS:
                _threadmem_upsert(channel, session_thread, text)
        finally:
            for r in recs:  # clear the whole group's queue (handled in one pass)
                try:
                    os.remove(r["_path"])
                except OSError:
                    pass


def notify_owner_of_dm(from_uid, from_name, text):
    """A non-owner DM'd the bot — forward a heads-up to the operator(s), then
    stay silent to the sender (DMs remain owner-only)."""
    body = " ".join((text or "").split())[:500] or "(空消息)"
    for oid in OWNER_SLACK_IDS:
        if oid == from_uid:
            continue
        try:
            ch = api("conversations.open", users=oid)["channel"]["id"]
            post(ch, f"📩 *{from_name}* (`{from_uid}`) 私信了我(非 owner,我未回复):\n> {body}")
        except Exception as exc:
            print(f"[slack] owner-notify failed: {exc}", flush=True)


def handle(ev, is_dm):
    channel, user, ts = ev["channel"], ev["user"], ev["ts"]
    person = b.person_for("slack", user)
    if is_dm and person not in OWNER_PERSONS:
        notify_owner_of_dm(user, user_name(user), ev.get("text") or "")
        return  # DMs are owner-only, same policy as Discord
    text = re.sub(rf"<@{SELF_ID}>", "", ev.get("text") or "").strip()
    author = user_name(user)
    owner_dm = is_dm and person in OWNER_PERSONS
    thread_ts = ev.get("thread_ts")  # reply in-thread only if asked in a thread
    # Download images before the limit check (not after) so a queued record still
    # has them available for the drain to replay later.
    images = download_images(ev)
    if not text:
        text = ("The user sent image(s) with no text — look at the attached image(s) and respond."
                if images else
                "The user only mentioned you without extra text. Reply briefly and ask what they need.")
    # SLACK_THREAD_SESSIONS routing: off keeps reply_thread_ts/key/thinking_ts
    # exactly as before (thread_ts, flat "slack:{channel}", ts) — see
    # _route_session()'s docstring for the on-behavior.
    if SLACK_THREAD_SESSIONS:
        session_thread, reply_thread_ts, key = _route_session(channel, ts, thread_ts, is_dm)
    else:
        session_thread, reply_thread_ts, key = None, thread_ts, f"slack:{channel}"
    if b.is_limited():
        defer_slack_message(channel, ts, thread_ts, user, author, person, text, is_dm, owner_dm, images)
        post(channel, f"⏳ Claude 额度暂时用满,约 {b.fmt_utc(b.limited_until())} 恢复后我会自动回复你。", reply_thread_ts)
        return
    print(f"[slack] handling {ts} in {channel} from {author} ({len(images)} img)", flush=True)
    thinking_ts = (reply_thread_ts or ts) if SLACK_THREAD_SESSIONS else ts
    _maybe_set_thread_title(channel, thinking_ts, text)
    if SLACK_LIVE_THINKING:
        # Pin a single steady "received, working" line from the very first
        # moment. Pass a one-element `loading` so this immediately overrides any
        # rotation lingering from a prior message on this thread (a plain
        # text-only status can't clear a sticky loading_messages — see
        # set_thinking()'s docstring). This kills the jarring flash of the old
        # three-line loop before the first real phase update arrives.
        set_thinking(channel, thinking_ts, "💭 已收到,正在处理…", loading=["💭 已收到,正在处理…"])
    else:
        set_thinking(channel, thinking_ts, loading=["正在理解你的消息…", "翻查上下文…", "组织回复…"])
    if SLACK_THREAD_SESSIONS and session_thread:
        history = fetch_thread_history(channel, session_thread, ts)
    else:
        history = fetch_history(channel, ts)
    crossctx = b.crossctx_block(person, "slack", channel)
    threadmem_content = _threadmem_read_block(channel) if SLACK_THREAD_SESSIONS else ""
    threadmem_path = _threadmem_path(channel) if SLACK_THREAD_SESSIONS else None
    viewing_channel = _lookup_viewing_channel(channel, thread_ts)
    instruction = build_instruction(author, channel, text, history, crossctx, is_dm, owner_dm, images,
                                     threadmem_content=threadmem_content, threadmem_path=threadmem_path,
                                     reply_thread_ts=reply_thread_ts, viewing_channel=viewing_channel)
    progress_cb = _make_progress_cb(channel, thinking_ts) if SLACK_LIVE_THINKING else None
    # SLACK_STREAMING: stream the answer text itself via chat.*Stream as it's
    # generated, instead of posting the full reply once run_claude returns.
    # Anchor the stream under the triggering message when we're not already
    # replying in a thread (reply_thread_ts is None) — chat.startStream's
    # thread_ts otherwise has nothing to attach to.
    streamer = None
    if SLACK_STREAMING:
        stream_thread_ts = reply_thread_ts or ts
        streamer = _Streamer(channel, stream_thread_ts, user,
                              on_clear=lambda: set_thinking(channel, thinking_ts, ""))
    try:
        with b.channel_lock(key):
            reply = b.run_claude(author, key, text, guild_id="_slack",
                                 is_dm=is_dm, owner_dm=owner_dm, instruction=instruction,
                                 on_progress=progress_cb,
                                 on_text=streamer.on_text if streamer else None)
    except b.RateLimited as rl:
        if streamer is not None:
            streamer.stop()  # best-effort close of any partial stream
        defer_slack_message(channel, ts, thread_ts, user, author, person, text, is_dm, owner_dm, images)
        set_thinking(channel, thinking_ts, "")
        post(channel, f"⏳ Claude 额度刚好用满,约 {b.fmt_utc(rl.reset_epoch)} 恢复后我会自动回复你。", reply_thread_ts)
        return
    except Exception as exc:
        if streamer is not None:
            streamer.stop()
        print(f"[slack] handler error in {channel}: {exc}", flush=True)
        set_thinking(channel, thinking_ts, "")
        post(channel, f"⚠️ bridge error: {str(exc)[:300]}", reply_thread_ts)
        return
    set_thinking(channel, thinking_ts, "")  # clear before the reply lands (also auto-clears)
    if streamer is None or not streamer.stop():
        # No streaming, or the stream never started/broke mid-way — the user
        # must still get the complete answer via the normal path.
        post(channel, reply, reply_thread_ts)
    b.log_crossctx(person, "slack", channel, text, reply)
    if SLACK_THREAD_SESSIONS:
        _threadmem_upsert(channel, session_thread, text)


def on_event(ev):
    et = ev.get("type")
    print(f"[slack] event: {et} ch={ev.get('channel')} user={ev.get('user')} "
          f"subtype={ev.get('subtype')}", flush=True)
    if et in ("assistant_thread_started", "assistant_thread_context_changed"):
        # Different payload shape (no top-level channel/user/ts) — dispatch
        # before the generic app_mention/message filtering below, which
        # assumes those fields exist.
        POOL.submit(on_assistant_thread_event, ev)
        return
    subtype = ev.get("subtype")
    if ev.get("bot_id") or (subtype and subtype != "file_share"):
        return  # other bots, edits, joins, … — but keep file_share (image uploads)
    user = ev.get("user")
    if not user or user == SELF_ID:
        return
    key = (ev.get("channel"), ev.get("ts"))
    if key in _seen:
        return
    _seen.add(key)
    if len(_seen) > 2000:
        _seen.clear()
    if et == "app_mention":
        POOL.submit(handle, ev, False)
    elif et == "message" and ev.get("channel_type") == "im":
        POOL.submit(handle, ev, True)


async def session():
    r = httpx.post(f"{API}/apps.connections.open",
                   headers={"Authorization": f"Bearer {APP_TOKEN}"}, timeout=20).json()
    if not r.get("ok"):
        raise RuntimeError(f"apps.connections.open: {r.get('error')}")
    async with websockets.connect(r["url"], max_size=None) as ws:
        async for raw in ws:
            m = json.loads(raw)
            if m.get("envelope_id"):  # MUST ack fast or Slack redelivers
                await ws.send(json.dumps({"envelope_id": m["envelope_id"]}))
            t = m.get("type")
            if t == "hello":
                print(f"[slack] READY — {b.AGENT_NAME} connected (Socket Mode)", flush=True)
            elif t == "disconnect":
                print(f"[slack] server asked to reconnect ({(m.get('reason') or '?')})", flush=True)
                return
            elif t == "events_api":
                try:
                    on_event((m.get("payload") or {}).get("event") or {})
                except Exception as exc:
                    print(f"[slack] event error: {exc}", flush=True)


async def main_async():
    global SELF_ID, TEAM_ID
    auth = api("auth.test")
    SELF_ID = auth["user_id"]
    TEAM_ID = auth.get("team_id")
    print(f"[slack] starting (bot user {SELF_ID}, workers={WORKERS})", flush=True)
    while True:
        try:
            await session()
        except Exception as exc:
            print(f"[slack] connection error: {exc} — reconnecting in 5s", flush=True)
        await asyncio.sleep(5)


if __name__ == "__main__":
    if not (BOT_TOKEN and APP_TOKEN):
        print("[slack] SLACK_BOT_TOKEN / SLACK_APP_TOKEN not set — bridge disabled", flush=True)
        sys.exit(0)
    asyncio.run(main_async())
