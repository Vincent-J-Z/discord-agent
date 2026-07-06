"""Slack toolbox — one CLI over the Slack Web API (the Slack twin of
discord_api.py). Used by the agent for anything Slack-side.

    python /app/src/slack_api.py whoami
    python /app/src/slack_api.py channels                    # channels the bot can see
    python /app/src/slack_api.py read  <channel> [--limit 30]
    python /app/src/slack_api.py post  <channel> "text" [--thread <ts>]
    python /app/src/slack_api.py react <channel> <ts> <emoji_name>   # e.g. eyes

Channel ids look like C…/G… (channels) or D… (DMs). Threads are addressed by the
parent message's ts via --thread.
"""
import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

_H = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", "/workspace")
load_dotenv(os.path.join(_H, ".env"))
load_dotenv(os.path.join(_WORKSPACE, ".env"), override=True)

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("SLACK_BOT_TOKEN is not set")
API = "https://slack.com/api"
H = {"Authorization": f"Bearer {TOKEN}"}

_user_cache = {}


def _call(method, **params):
    r = httpx.post(f"{API}/{method}", headers={**H, "Content-Type": "application/json; charset=utf-8"},
                   json=params or {}, timeout=20)
    d = r.json()
    if not d.get("ok"):
        raise SystemExit(f"{method} -> {d.get('error')}")
    return d


def _name(uid):
    if not uid:
        return "?"
    if uid not in _user_cache:
        try:
            u = _call("users.info", user=uid)["user"]
            _user_cache[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name") or uid
        except SystemExit:
            _user_cache[uid] = uid
    return _user_cache[uid]


def whoami(_):
    d = _call("auth.test")
    print(f"{d.get('user')} (id={d.get('user_id')}) team: {d.get('team')}")


def channels(_):
    cursor = None
    while True:
        kw = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kw["cursor"] = cursor
        d = _call("conversations.list", **kw)
        for c in d.get("channels", []):
            member = "member" if c.get("is_member") else "not-member"
            print(f"{c['id']}  {member:<10} #{c.get('name')}")
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break


def read(a):
    d = _call("conversations.history", channel=a.channel, limit=a.limit)
    for m in sorted(d.get("messages", []), key=lambda x: float(x.get("ts", 0))):
        body = " ".join((m.get("text") or "").split())
        print(f"{m.get('ts')} [{_name(m.get('user') or m.get('bot_id'))}] {body}")


def post(a):
    kw = {"channel": a.channel, "text": a.text[:39000]}
    if a.thread:
        kw["thread_ts"] = a.thread
    d = _call("chat.postMessage", **kw)
    print(d.get("ts"))


def react(a):
    _call("reactions.add", channel=a.channel, timestamp=a.ts, name=a.emoji.strip(":"))
    print("ok")


def main():
    p = argparse.ArgumentParser(description="Slack agent toolbox")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami").set_defaults(fn=whoami)
    sub.add_parser("channels").set_defaults(fn=channels)
    r = sub.add_parser("read")
    r.add_argument("channel")
    r.add_argument("--limit", type=int, default=30)
    r.set_defaults(fn=read)
    po = sub.add_parser("post")
    po.add_argument("channel")
    po.add_argument("text")
    po.add_argument("--thread", default="")
    po.set_defaults(fn=post)
    re_ = sub.add_parser("react")
    re_.add_argument("channel")
    re_.add_argument("ts")
    re_.add_argument("emoji")
    re_.set_defaults(fn=react)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
