"""Hourly proactive sweep — per server, fully isolated.

Gathers new activity since the last sweep and reviews EACH server separately:
its own Claude session (`__sweep__<guild_id>`), its own toolbox scope
(MOCHI_CURRENT_GUILD), and it only ever acts within that server. No server's
activity is ever mixed with, or surfaced in, another. Triggered by
discord_agent_runtime on a timer.

Anti-spam by design: stays silent unless something in THAT server genuinely needs
a reply/action; then it acts in the relevant channel of that same server.
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
    """Return {guild_id: [(channel_id, [lines]), ...]} of new activity since the
    last sweep, advancing per-channel cursors. New channels start at 'now'."""
    cursors = _load()
    try:
        channels = b.list_text_channels()  # also populates b.CHANNEL_GUILD
    except Exception as exc:
        print(f"[sweep] channel list error: {exc}", flush=True)
        return {}, cursors
    by_guild = {}
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
            author = m.get("author") or {}
            if author.get("id") == b.BOT_ID:
                continue
            body = b.WS_RE.sub(" ", m.get("content", "") or "").strip()
            if not body:
                continue
            lines.append(f"[{author.get('username', '?')}] {body[:500]}")
        if lines:
            gid = b.CHANNEL_GUILD.get(ch, "unknown")
            by_guild.setdefault(gid, []).append((ch, lines[-MAX_PER_CHANNEL:]))
    return by_guild, cursors


def run_sweep(activity, guild_id):
    session_key = f"__sweep__{guild_id}"
    session_id = b.get_session(session_key)
    instruction = (
        "You are Mochi_Bot doing your scheduled hourly review of this Discord "
        "server. Below is the NEW activity in it since your last review.\n"
        "Stay within this server and your working directory; the toolbox is "
        "scoped to this server. Never reference, reveal, or act on anything "
        "outside it (other deployments, paths, or directory layout).\n"
        "1) Understand what's happening (relate to prior reviews of this server).\n"
        "2) Decide whether anything genuinely needs a response/action FROM YOU.\n"
        "3) If YES — act in the relevant channel of THIS server (discord_api.py). "
        "If NO — do NOTHING and post NOTHING; stay silent.\n"
        "Default STRONGLY to silence: most hours warrant no message. Don't post "
        "digests or 'quiet hour' notes. Your text output is just logged.\n\n"
        f"NEW ACTIVITY:\n{activity}"
    )
    server_dir = b.ensure_server_dir(guild_id)
    sub_env = dict(
        os.environ,
        MOCHI_CURRENT_GUILD=str(guild_id),
        MOCHI_SERVER_DIR=server_dir,
        CLAUDE_CONFIG_DIR=os.path.join(server_dir, ".claude"),
        TMPDIR=os.path.join(server_dir, "tmp"),
    )
    cmd = [b.CLAUDE_BIN, "-p", "--permission-mode", b.PERMISSION_MODE, "--output-format", "json"]
    if b.CLAUDE_MODEL:
        cmd.extend(["--model", b.CLAUDE_MODEL])
    if b.CLAUDE_EFFORT:
        cmd.extend(["--effort", b.CLAUDE_EFFORT])
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(instruction)

    def run(c):
        return subprocess.run(c, cwd=server_dir, text=True, env=sub_env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=b.TIMEOUT_SECONDS)

    result = run(cmd)
    if result.returncode != 0 and session_id:  # stale session → retry fresh
        b.clear_session(session_key)
        result = run([c for c in cmd if c not in (session_id, "--resume")])
    if result.returncode != 0:
        print(f"[sweep] {guild_id} claude failed: {(result.stderr or result.stdout)[:200]}", flush=True)
        return
    try:
        data = json.loads(result.stdout or "{}")
        if data.get("session_id"):
            b.set_session(session_key, data["session_id"])
        print(f"[sweep] {guild_id} done: {(data.get('result') or '')[:160]}", flush=True)
    except Exception:
        print(f"[sweep] {guild_id} done (unparsed output)", flush=True)


def main():
    if b.is_limited():
        print("[sweep] skipped — rate-limited", flush=True)
        return
    by_guild, cursors = gather()
    _save(cursors)
    if not by_guild:
        print("[sweep] no new activity since last sweep", flush=True)
        return
    for guild_id, chans in by_guild.items():
        if b.is_limited():
            print("[sweep] rate-limited mid-sweep — stopping", flush=True)
            break
        blocks = [f"== channel {ch} ({len(lines)} new) ==\n" + "\n".join(lines) for ch, lines in chans]
        n = sum(len(lines) for _, lines in chans)
        print(f"[sweep] server {guild_id}: {n} new msg(s) across {len(chans)} channel(s) — reviewing", flush=True)
        try:
            run_sweep("\n\n".join(blocks), guild_id)
        except subprocess.TimeoutExpired:
            print(f"[sweep] {guild_id} review timed out", flush=True)


if __name__ == "__main__":
    main()
