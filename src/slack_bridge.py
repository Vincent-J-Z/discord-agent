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

# Persons linked to a Discord owner id are owners here too (same human).
OWNER_PERSONS = {p for p in (b.person_for("discord", oid) for oid in b.OWNER_IDS) if p}

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
    text = (text or "").strip() or "(no output)"
    while text:
        chunk, text = text[:3900], text[3900:]
        kw = {"channel": channel, "text": chunk}
        if thread_ts:
            kw["thread_ts"] = thread_ts
        try:
            api("chat.postMessage", **kw)
        except Exception as exc:
            print(f"[slack] post failed in {channel}: {exc}", flush=True)
            return


def react(channel, ts, name):
    try:
        api("reactions.add", channel=channel, timestamp=ts, name=name)
    except Exception:
        pass  # already_reacted etc. — cosmetic


def fetch_history(channel, before_ts):
    """Recent messages (oldest first) for context, excluding the trigger itself."""
    try:
        d = api("conversations.history", channel=channel, limit=HISTORY_LIMIT + 1)
    except Exception as exc:
        print(f"[slack] history fetch failed: {exc}", flush=True)
        return ""
    lines = []
    for m in sorted(d.get("messages", []), key=lambda x: float(x.get("ts", 0))):
        if m.get("ts") == before_ts or m.get("subtype"):
            continue
        body = " ".join((m.get("text") or "").split())
        if not body:
            continue
        lines.append(f"[{user_name(m.get('user') or m.get('bot_id'))}] {body[:500]}")
    return "\n".join(lines[-HISTORY_LIMIT:])


def build_instruction(author, channel, prompt, history, crossctx, is_dm, owner_dm):
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
        "Use the recent messages for context; act on the new Message; reply "
        "concisely in plain text. For longer work, post brief progress updates "
        f"as you go (`python /app/src/slack_api.py post {channel} \"...\"`); the "
        "bridge only posts your final answer."
    )
    return ("\n".join(lines)
            + f"\n\nSender: {author}\nThis Slack channel: {channel}\n\n"
            + f"{crossctx}{context_block}New Message:\n{prompt}")


def handle(ev, is_dm):
    channel, user, ts = ev["channel"], ev["user"], ev["ts"]
    person = b.person_for("slack", user)
    if is_dm and person not in OWNER_PERSONS:
        return  # DMs are owner-only, same policy as Discord
    text = re.sub(rf"<@{SELF_ID}>", "", ev.get("text") or "").strip()
    author = user_name(user)
    owner_dm = is_dm and person in OWNER_PERSONS
    thread_ts = ev.get("thread_ts")  # reply in-thread only if asked in a thread
    react(channel, ts, "eyes")
    if b.is_limited():
        post(channel, f"⏳ Claude 额度暂时用满,约 {b.fmt_utc(b.limited_until())} 恢复。", thread_ts)
        return
    if not text:
        text = "The user only mentioned you without extra text. Reply briefly and ask what they need."
    print(f"[slack] handling {ts} in {channel} from {author}", flush=True)
    history = fetch_history(channel, ts)
    crossctx = b.crossctx_block(person, "slack", channel)
    instruction = build_instruction(author, channel, text, history, crossctx, is_dm, owner_dm)
    key = f"slack:{channel}"
    try:
        with b.channel_lock(key):
            reply = b.run_claude(author, key, text, guild_id="_slack",
                                 is_dm=is_dm, owner_dm=owner_dm, instruction=instruction)
    except b.RateLimited as rl:
        post(channel, f"⏳ Claude 额度用满,约 {b.fmt_utc(rl.reset)} 恢复。", thread_ts)
        return
    except Exception as exc:
        print(f"[slack] handler error in {channel}: {exc}", flush=True)
        post(channel, f"⚠️ bridge error: {str(exc)[:300]}", thread_ts)
        return
    post(channel, reply, thread_ts)
    b.log_crossctx(person, "slack", channel, text, reply)


def on_event(ev):
    et = ev.get("type")
    if ev.get("bot_id") or ev.get("subtype"):
        return  # other bots, edits, joins, …
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
