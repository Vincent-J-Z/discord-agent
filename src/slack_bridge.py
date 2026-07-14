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
groups:history im:history im:read reactions:write users:read.
"""
import asyncio
import concurrent.futures
import json
import os
import re
import subprocess
import sys
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

# Persons linked to a Discord owner id are owners here too (same human).
OWNER_PERSONS = {p for p in (b.person_for("discord", oid) for oid in b.OWNER_IDS) if p}
# Slack uids of those owners — so we can DM the operator a heads-up.
OWNER_SLACK_IDS = {uid for (plat, uid), name in b.USER_LINKS.items()
                   if plat == "slack" and name in OWNER_PERSONS}

SELF_ID = None
_user_cache = {}
_seen = set()  # (channel, ts) — Socket Mode may redeliver


def api(method, **params):
    r = httpx.post(f"{API}/{method}",
                   headers={"Authorization": f"Bearer {BOT_TOKEN}",
                            "Content-Type": "application/json; charset=utf-8"},
                   json=params or {}, timeout=20)
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


def react(channel, ts, name):
    try:
        api("reactions.add", channel=channel, timestamp=ts, name=name)
    except Exception:
        pass  # already_reacted etc. — cosmetic


def unreact(channel, ts, name):
    try:
        api("reactions.remove", channel=channel, timestamp=ts, name=name)
    except Exception:
        pass  # no_reaction etc. — cosmetic


def set_status(channel, ts, done, old="eyes"):
    """Swap the trigger message's status reaction: 👀 while working →
    ✅ done / ⚠️ error. Keeps the outcome on the message, out of the reply text."""
    unreact(channel, ts, old)
    react(channel, ts, "white_check_mark" if done else "warning")


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


def build_instruction(author, channel, prompt, history, crossctx, is_dm, owner_dm, images=None):
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
    lines.append(
        "Use the recent messages for context; act on the new Message. Your final "
        "answer IS delivered to this channel in full — long answers are split into "
        "several messages automatically, so put the COMPLETE deliverable in it: the "
        "actual results, numbers, findings, and any decision you need from the "
        "user. NEVER leave the substance only in local files or session memory — "
        "the user cannot see your machine, so 'full log in tmp/x.txt' delivers "
        "nothing; quote the relevant content itself. For longer work, post brief "
        "progress updates as you go "
        f"(`python /app/src/slack_api.py post {channel} \"...\"`) so nothing stays "
        "invisible. You are the dispatcher, not the laborer: substantial work "
        "(builds, installs, pipelines, long jobs, real coding tasks) must be "
        "handed to a tmux worker — subagent.py claude <name> \"<brief>\" "
        "--channel <this channel> --report (see CLAUDE.md) — then supervised "
        "across turns with list/logs and course-corrected with `steer <name> "
        "\"...\"`; don't grind it inside this reply run. FIRST on every message run "
        "`python /app/src/subagent.py list --channel <this channel>` to see tasks "
        "already going here and route any follow-up to the right one (ask if "
        "ambiguous). Do NOT put a status/done "
        "checkmark (✅) in your reply text — the "
        "bridge already signals completion by swapping the 👀 reaction on the "
        "user's message to ✅. Keep the prose free of status emoji."
    )
    img_block = ""
    if images:
        img_block = (
            f"\n\n[发信人上传了 {len(images)} 张图片,已下载到本地。用 Read 工具"
            "查看下列绝对路径的图片,把图片内容纳入你的回答:\n"
            + "\n".join(f"- {p}" for p in images) + "\n]"
        )
    return ("\n".join(lines)
            + f"\n\nSender: {author}\nThis Slack channel: {channel}\n\n"
            + f"{crossctx}{context_block}New Message:\n{prompt}{img_block}")


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
    rate-limited again, same philosophy as the Discord drain."""
    groups = {}
    for path in list_deferred_slack():
        try:
            with open(path) as f:
                rec = json.load(f)
            rec["_path"] = path
            groups.setdefault((rec.get("channel"), rec.get("thread_ts")), []).append(rec)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
    for (channel, thread_ts), recs in groups.items():
        if b.is_limited():
            return  # still limited — leave the rest for next time
        recs.sort(key=lambda r: float(r.get("ts") or 0))
        latest = recs[-1]
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
        try:
            history = fetch_history(channel, latest.get("ts"))
        except Exception:
            history = ""
        crossctx = b.crossctx_block(person, "slack", channel)
        instruction = build_instruction(author, channel, catchup_text, history, crossctx,
                                         is_dm, owner_dm, images)
        key = f"slack:{channel}"
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
            post(channel, reply, thread_ts)
            unreact(channel, latest.get("ts"), "warning")
            set_status(channel, latest.get("ts"), done=True)
            b.log_crossctx(person, "slack", channel, text, reply)
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
    react(channel, ts, "eyes")
    # Download images before the limit check (not after) so a queued record still
    # has them available for the drain to replay later.
    images = download_images(ev)
    if not text:
        text = ("The user sent image(s) with no text — look at the attached image(s) and respond."
                if images else
                "The user only mentioned you without extra text. Reply briefly and ask what they need.")
    if b.is_limited():
        defer_slack_message(channel, ts, thread_ts, user, author, person, text, is_dm, owner_dm, images)
        set_status(channel, ts, done=False)
        post(channel, f"⏳ Claude 额度暂时用满,约 {b.fmt_utc(b.limited_until())} 恢复后我会自动回复你。", thread_ts)
        return
    print(f"[slack] handling {ts} in {channel} from {author} ({len(images)} img)", flush=True)
    history = fetch_history(channel, ts)
    crossctx = b.crossctx_block(person, "slack", channel)
    instruction = build_instruction(author, channel, text, history, crossctx, is_dm, owner_dm, images)
    key = f"slack:{channel}"
    try:
        with b.channel_lock(key):
            reply = b.run_claude(author, key, text, guild_id="_slack",
                                 is_dm=is_dm, owner_dm=owner_dm, instruction=instruction)
    except b.RateLimited as rl:
        defer_slack_message(channel, ts, thread_ts, user, author, person, text, is_dm, owner_dm, images)
        set_status(channel, ts, done=False)
        post(channel, f"⏳ Claude 额度刚好用满,约 {b.fmt_utc(rl.reset_epoch)} 恢复后我会自动回复你。", thread_ts)
        return
    except Exception as exc:
        print(f"[slack] handler error in {channel}: {exc}", flush=True)
        set_status(channel, ts, done=False)
        post(channel, f"⚠️ bridge error: {str(exc)[:300]}", thread_ts)
        return
    post(channel, reply, thread_ts)
    set_status(channel, ts, done=True)
    b.log_crossctx(person, "slack", channel, text, reply)


def on_event(ev):
    et = ev.get("type")
    print(f"[slack] event: {et} ch={ev.get('channel')} user={ev.get('user')} "
          f"subtype={ev.get('subtype')}", flush=True)
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
    global SELF_ID
    SELF_ID = api("auth.test")["user_id"]
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
