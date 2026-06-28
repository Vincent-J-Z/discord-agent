"""Maintain ONE live status-board message in the channel — post first time, then
EDIT in place each cron tick (no channel spam). Reads board text from
.status.md; stores the message id in .status_msg_id."""
import os
import httpx
from dotenv import load_dotenv

_H = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", _H)
os.makedirs(_WORKSPACE, exist_ok=True)
load_dotenv(os.path.join(_H, ".env"))
load_dotenv(os.path.join(_WORKSPACE, ".env"), override=True)
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CH = os.environ["DISCORD_CHANNEL_ID"]
H = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
MD = os.path.join(_WORKSPACE, ".status.md")
IDF = os.path.join(_WORKSPACE, ".status_msg_id")
BASE = f"https://discord.com/api/v10/channels/{CH}/messages"

from datetime import datetime
_now = datetime.now().strftime("%H:%M:%S")
txt = (open(MD).read().rstrip() + f"\n🕐 **更新于 {_now}**(每分钟刷新)")[:1990]
mid = open(IDF).read().strip() if os.path.exists(IDF) else ""
payload = {"content": txt, "allowed_mentions": {"parse": []}}

if mid:
    r = httpx.patch(f"{BASE}/{mid}", json=payload, headers=H, timeout=20)
    if r.status_code == 200:
        raise SystemExit  # silent on routine success — no per-tick log spam
# first time, or the message was deleted -> post a fresh one
r = httpx.post(BASE, json=payload, headers=H, timeout=20)
if r.status_code in (200, 201):
    open(IDF, "w").write(str(r.json()["id"]))
    print("status posted", r.json()["id"])
else:
    print("status ERR", r.status_code, r.text[:200])
