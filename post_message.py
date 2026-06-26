"""Post a message to the coordination channel via the Discord REST API.

Usage:
    python3 post_message.py "your message text"
    python3 post_message.py "hi <@USER_ID>" --mention USER_ID [--mention USER_ID ...]

For multi-line or templated posts, import send() from this module instead of
building f-strings with bare quotes on the command line.
"""
import os, sys, argparse
import httpx
from dotenv import load_dotenv

_H = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", _H)
load_dotenv(os.path.join(_H, ".env"))
load_dotenv(os.path.join(_WORKSPACE, ".env"), override=True)
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CH = os.environ["DISCORD_CHANNEL_ID"]
BASE = f"https://discord.com/api/v10/channels/{CH}/messages"
H = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}


def send(content, mention_users=None):
    """Post `content`. `mention_users` is a list of user ids to actually ping
    (allowed_mentions is otherwise empty, so stray <@id> text won't notify)."""
    payload = {
        "content": content[:1990],
        "allowed_mentions": {"users": list(mention_users or [])},
    }
    r = httpx.post(BASE, json=payload, headers=H, timeout=20)
    if r.status_code in (200, 201):
        return r.json()["id"]
    raise RuntimeError(f"post failed {r.status_code}: {r.text[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("content")
    ap.add_argument("--mention", action="append", default=[], help="user id to ping")
    a = ap.parse_args()
    print("posted", send(a.content, a.mention))


if __name__ == "__main__":
    main()
