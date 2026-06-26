"""One-shot Discord poll for the cron tick. Prints new relevant messages since
the persisted cursor and advances it, then exits. If the marker file
.discord_voice_on exists, also drops each message into .speak_queue/ (one JSON
per message, named by snowflake) for a standalone speaker daemon to read out —
the poll never does TTS or playback itself, so listening is never blocked by
speech."""
import os, re, json
import httpx
from dotenv import load_dotenv

_H = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", _H)
os.makedirs(_WORKSPACE, exist_ok=True)
load_dotenv(os.path.join(_H, ".env"))
load_dotenv(os.path.join(_WORKSPACE, ".env"), override=True)
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CH = os.environ["DISCORD_CHANNEL_ID"]     # coordination channel id
BOT = os.environ["DISCORD_BOT_ID"]        # this bot's user id (to skip our own messages)
ROLE = os.environ.get("DISCORD_ROLE_ID", "").strip()  # optional role mention trigger
SEEN = os.path.join(_WORKSPACE, ".discord_seen")
VOICE_MARKER = os.path.join(_WORKSPACE, ".discord_voice_on")
QDIR = os.path.join(_WORKSPACE, ".speak_queue")
H = {"Authorization": f"Bot {TOKEN}"}
_WS = re.compile(r"\s+")


def is_noise(c):
    s = (c or "").strip().strip("_").strip()
    return s == "" or s.startswith("🔧")


def enqueue_speech(mid, author, content):
    os.makedirs(QDIR, exist_ok=True)
    dst = os.path.join(QDIR, f"{mid}.json")
    tmp = dst + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"author": author, "content": content}, f, ensure_ascii=False)
    os.replace(tmp, dst)   # atomic; daemon only sees fully-written files


def main():
    last = open(SEEN).read().strip() if os.path.exists(SEEN) else "0"
    with httpx.Client(timeout=20, headers=H) as c:
        msgs = c.get(f"https://discord.com/api/v10/channels/{CH}/messages",
                     params={"limit": 100, "after": last}).json()
    if not isinstance(msgs, list) or not msgs:
        print("[poll] no new messages")
        return
    ms = sorted(msgs, key=lambda m: int(m["id"]))
    out = []
    voice_on = os.path.exists(VOICE_MARKER)
    for d in ms:
        last = d["id"]
        if d["author"]["id"] == BOT or is_noise(d.get("content", "")):
            continue
        body = _WS.sub(" ", d.get("content", "")).strip()
        atme = (f"<@{BOT}>" in d.get("content", "") or f"<@!{BOT}>" in d.get("content", "")
                or (ROLE and f"<@&{ROLE}>" in d.get("content", "")))
        line = ("@ME " if atme else "") + f"[{d['author']['username']}] {body[:600]}"
        atts = d.get("attachments") or []
        if atts:
            line += "  📎ATTACH: " + " | ".join(f"{a.get('filename')} <{a.get('url')}>" for a in atts)
        out.append(line)
        if voice_on:
            enqueue_speech(d["id"], d["author"]["username"], d.get("content", ""))
    with open(SEEN, "w") as f:
        f.write(last)
    print(f"[poll] {len(out)} new relevant message(s):")
    for line in out:
        print("  " + line)


if __name__ == "__main__":
    main()
