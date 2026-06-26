"""Discord Gateway: presence + real-time @-mention trigger.

Holds one Gateway WebSocket that (1) keeps the bot ONLINE and (2) subscribes to
guild messages, so a new message is PUSHED to us instantly instead of being
discovered by the 60s REST poll. Messages that @-mention the bot are dispatched
to a thread pool running the SAME handler as the poller — near-instant and
concurrent, so one heavy task no longer blocks others or the heartbeat.

The REST poller in discord_claude_bridge.py stays as a fallback; the shared
atomic dedup (bridge.claim_message) guarantees no double replies.

intents = GUILD_MESSAGES (1<<9), which is NOT privileged. @-mention messages
carry their content even without the privileged Message Content intent, which is
all an @-triggered bot needs — so no Developer-Portal change is required. (If
content ever comes through empty for mentions, enable "Message Content Intent"
in the portal and add 1<<15 to INTENTS.)
"""
import asyncio
import concurrent.futures
import json
import os
import sys

import websockets
from dotenv import load_dotenv

import discord_claude_bridge as bridge

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", os.path.expanduser("~/discordAgentWorkspace"))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
load_dotenv(os.path.join(WORKSPACE, "secrets.env"), override=True)

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
STATUS = os.environ.get("BOT_STATUS", "online")
ACTIVITY_NAME = os.environ.get("BOT_ACTIVITY", "for @mentions")
ACTIVITY_TYPE = int(os.environ.get("BOT_ACTIVITY_TYPE", "3"))
# GUILD_MESSAGES lets Discord push MESSAGE_CREATE. Non-privileged.
INTENTS = int(os.environ.get("GATEWAY_INTENTS", str(1 << 9)))
WORKERS = int(os.environ.get("GATEWAY_WORKERS", "4"))
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS)

SELF = os.path.abspath(__file__)
WATCH = [SELF, os.path.join(ROOT, "discord_claude_bridge.py")]


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


MT = {p: _mtime(p) for p in WATCH}


def identify_payload():
    return {
        "op": 2,
        "d": {
            "token": TOKEN,
            "intents": INTENTS,
            "properties": {"os": "linux", "browser": "mochi", "device": "mochi"},
            "presence": {
                "status": STATUS,
                "since": 0,
                "afk": False,
                "activities": [{"name": ACTIVITY_NAME, "type": ACTIVITY_TYPE}],
            },
        },
    }


def _handle(channel_id, d):
    try:
        bridge.handle_message(channel_id, d)
    except Exception as exc:
        print(f"[gateway] handler error in {channel_id}: {exc}", flush=True)


def on_message_create(d):
    # Cheap pre-filter: only @-mentions of the bot, never our own messages.
    if (d.get("author") or {}).get("id") == bridge.BOT_ID:
        return
    if not bridge.is_addressed(d.get("content", "") or ""):
        return
    channel_id = str(d.get("channel_id"))
    who = (d.get("author") or {}).get("username")
    print(f"[gateway] @mention in {channel_id} from {who} — dispatching", flush=True)
    POOL.submit(_handle, channel_id, d)


async def heartbeat(ws, interval, get_seq):
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1, "d": get_seq()}))


async def reload_watcher():
    """Re-exec on source change (own or the bridge it imports) so the gateway
    hot-reloads like the bridge does."""
    while True:
        await asyncio.sleep(5)
        if not any(_mtime(p) > MT.get(p, 0.0) for p in WATCH):
            continue
        ok = True
        for f in WATCH:
            try:
                with open(f) as fh:
                    compile(fh.read(), f, "exec")
            except SyntaxError as exc:
                print(f"[gateway] reload skipped — syntax error in {os.path.basename(f)}: {exc}", flush=True)
                MT[f] = _mtime(f)
                ok = False
        if ok:
            print("[gateway] source changed — reloading (execv)", flush=True)
            POOL.shutdown(wait=False)
            os.execv(sys.executable, [sys.executable, SELF])


async def session():
    async with websockets.connect(GATEWAY, max_size=None) as ws:
        hello = json.loads(await ws.recv())
        interval = hello["d"]["heartbeat_interval"] / 1000.0
        seq = {"v": None}
        await ws.send(json.dumps(identify_payload()))
        hb = asyncio.create_task(heartbeat(ws, interval, lambda: seq["v"]))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    seq["v"] = msg["s"]
                op = msg.get("op")
                if op == 1:  # heartbeat requested
                    await ws.send(json.dumps({"op": 1, "d": seq["v"]}))
                elif op in (7, 9):  # reconnect / invalid session
                    print(f"[gateway] op {op} — reconnecting", flush=True)
                    return
                elif op == 0:
                    t = msg.get("t")
                    if t == "READY":
                        print(f"[gateway] READY — {msg['d']['user']['username']} is ONLINE "
                              f"(listening for @mentions, intents={INTENTS})", flush=True)
                    elif t == "MESSAGE_CREATE":
                        on_message_create(msg["d"])
        finally:
            hb.cancel()


async def run():
    asyncio.create_task(reload_watcher())
    while True:
        try:
            await session()
            print("[gateway] connection closed; reconnecting in 5s", flush=True)
        except Exception as exc:
            print(f"[gateway] error: {exc}; reconnecting in 5s", flush=True)
        await asyncio.sleep(5)


if __name__ == "__main__":
    print("[gateway] starting (presence + @mention trigger)", flush=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
