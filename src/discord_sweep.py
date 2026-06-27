"""Hourly proactive sweep.

Gathers new activity across all watched channels/threads since the last sweep,
then lets Claude (with its own persistent session, so it remembers prior sweeps)
summarize it, assess whether anything needs attention/action, and decide whether
to speak up — conservatively. Triggered by discord_agent_runtime on a timer.

Anti-spam by design: it posts a short digest to a REPORT channel (default: the
maintenance channel) and only interjects elsewhere for genuinely important
things. Default is to stay quiet.
"""
import json
import os
import subprocess

from dotenv import load_dotenv

import discord_claude_bridge as b

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", os.path.expanduser("~/discordAgentWorkspace"))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
load_dotenv(os.path.join(WORKSPACE, "secrets.env"), override=True)

SWEEP_CURSORS = os.path.join(WORKSPACE, ".sweep_cursors.json")
REPORT_CHANNEL = os.environ.get("SWEEP_REPORT_CHANNEL", "").strip() or b.CHANNEL_ID
SWEEP_SESSION_KEY = "__sweep__"
MAX_PER_CHANNEL = int(os.environ.get("SWEEP_MAX_PER_CHANNEL", "40"))


def _load():
    if os.path.exists(SWEEP_CURSORS):
        try:
            with open(SWEEP_CURSORS) as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def _save(cursors):
    tmp = SWEEP_CURSORS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cursors, f)
    os.replace(tmp, SWEEP_CURSORS)


def gather():
    """Return [(channel_id, [lines])] of new, non-noise activity since last sweep,
    advancing per-channel cursors. New channels start at 'now' (no backlog)."""
    cursors = _load()
    digest = []
    try:
        channels = b.list_text_channels()
    except Exception as exc:
        print(f"[sweep] channel list error: {exc}", flush=True)
        return [], cursors
    for ch in channels:
        try:
            if ch not in cursors:
                cursors[ch] = b.fetch_latest_id(ch)
                continue
            msgs = b.fetch_messages(ch, cursors[ch])
        except Exception as exc:
            print(f"[sweep] fetch {ch} error: {exc}", flush=True)
            continue
        lines = []
        for m in msgs:
            cursors[ch] = m["id"]
            author = (m.get("author") or {})
            if author.get("id") == b.BOT_ID:
                continue
            body = b.WS_RE.sub(" ", m.get("content", "") or "").strip()
            if not body:
                continue
            lines.append(f"[{author.get('username', '?')}] {body[:500]}")
        if lines:
            digest.append((ch, lines[-MAX_PER_CHANNEL:]))
    return digest, cursors


def run_sweep(activity):
    session_id = b.get_session(SWEEP_SESSION_KEY)
    instruction = (
        "You are Mochi_Bot doing your scheduled hourly review of this Discord "
        "server. Below is the NEW activity since your last review.\n"
        "1) Understand what's happening (relate it to prior reviews).\n"
        "2) Decide whether anything genuinely needs a response or action FROM "
        "YOU: an unanswered question you can answer, a task you should do or "
        "flag, a problem you can help with.\n"
        "3) If YES — respond or act in the RELEVANT channel using your tools "
        "(discord_api.py). If NO — do NOTHING and post NOTHING; stay silent.\n"
        "Default STRONGLY to silence: most hours warrant no message at all. Do "
        "NOT post digests, summaries, or 'quiet hour' notes — silence is the "
        "correct output when nothing needs you. Only speak when it clearly adds "
        "value, and keep it brief. (Reply your one-line internal conclusion as "
        "your text output — that's just logged, not posted to Discord.)\n"
        f"(For a proactive note not tied to a specific channel, use channel {REPORT_CHANNEL}.)\n\n"
        f"NEW ACTIVITY SINCE LAST REVIEW:\n{activity}"
    )
    cmd = [b.CLAUDE_BIN, "-p", "--permission-mode", b.PERMISSION_MODE, "--output-format", "json"]
    if b.CLAUDE_MODEL:
        cmd.extend(["--model", b.CLAUDE_MODEL])
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(instruction)
    result = subprocess.run(
        cmd, cwd=b.CLAUDE_CWD, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=b.TIMEOUT_SECONDS,
    )
    if result.returncode != 0 and session_id:  # stale session → retry fresh
        b.clear_session(SWEEP_SESSION_KEY)
        cmd = [c for c in cmd if c != session_id and c != "--resume"]
        result = subprocess.run(
            cmd, cwd=b.CLAUDE_CWD, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=b.TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        print(f"[sweep] claude failed: {(result.stderr or result.stdout)[:300]}", flush=True)
        return
    try:
        data = json.loads(result.stdout or "{}")
        if data.get("session_id"):
            b.set_session(SWEEP_SESSION_KEY, data["session_id"])
        print(f"[sweep] done: {(data.get('result') or '')[:200]}", flush=True)
    except Exception:
        print("[sweep] done (unparsed output)", flush=True)


def main():
    if b.is_limited():
        print("[sweep] skipped — rate-limited", flush=True)
        return
    digest, cursors = gather()
    _save(cursors)
    if not digest:
        print("[sweep] no new activity since last sweep", flush=True)
        return
    blocks = []
    for ch, lines in digest:
        blocks.append(f"== channel {ch} ({len(lines)} new) ==\n" + "\n".join(lines))
    activity = "\n\n".join(blocks)
    print(f"[sweep] {sum(len(l) for _, l in digest)} new msgs across {len(digest)} channels — reviewing", flush=True)
    try:
        run_sweep(activity)
    except subprocess.TimeoutExpired:
        print("[sweep] review timed out", flush=True)


if __name__ == "__main__":
    main()
