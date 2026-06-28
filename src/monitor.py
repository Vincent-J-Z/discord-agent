#!/usr/bin/env python3
"""Mochi_Bot live monitor — a multi-panel terminal dashboard.

Run inside the container (reads /workspace telemetry only — no hooks into the
bot, so it can't break anything):

    container exec -it discord-agent python /app/src/monitor.py

Top panel: worker utilization (n/N working), sessions, rate-limit, today's cost.
Middle: one live panel per running agent showing its current phase + thinking /
tool-use. Bottom: recent completed runs. Ctrl-C to quit.
"""
import glob
import json
import os
import time

from dotenv import load_dotenv
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", "/workspace")
RUNS_DIR = os.path.join(WORKSPACE, ".runs")
# Read the real config the bridge/gateway use, not just the exec environment.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
WORKERS = int(os.environ.get("GATEWAY_WORKERS", "4"))
PHASE_STYLE = {"💭 thinking": "yellow", "✍️ replying": "green", "done": "dim", "starting": "cyan"}


def _json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _runs():
    out = []
    for f in glob.glob(os.path.join(RUNS_DIR, "*.json")):
        d = _json(f, None)
        if not d:
            continue
        pid = d.get("pid")
        if pid and not _alive(pid):  # process gone — stale telemetry, clean up
            try:
                os.remove(f)
            except OSError:
                pass
            continue
        out.append(d)
    return sorted(out, key=lambda d: d.get("start", 0))


def _usage():
    rows = []
    try:
        with open(os.path.join(WORKSPACE, ".usage.jsonl")) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def _dur(s):
    s = int(s)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _short(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def overview(runs):
    now = time.time()
    busy = len(runs)
    try:
        lim = float(open(os.path.join(WORKSPACE, ".limited_until")).read().strip())
    except Exception:
        lim = 0
    deferred = len(glob.glob(os.path.join(WORKSPACE, ".deferred", "*.json")))
    sessions = len(_json(os.path.join(WORKSPACE, ".sessions.json"), {}))
    watched = len(_json(os.path.join(WORKSPACE, ".bridge_cursors.json"), {}))
    u = _usage()
    today = [r for r in u if now - (r.get("ts") or 0) < 86400]
    spent = sum(r.get("cost_usd") or 0 for r in today)
    bar = "█" * busy + "░" * max(0, WORKERS - busy)
    barcol = "red" if busy >= WORKERS else "green"
    lim_s = f"[red]limited→{time.strftime('%H:%M UTC', time.gmtime(lim))}[/]" if lim > now else "[green]ok[/]"
    t = Table.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(justify="right", ratio=1)
    t.add_row(f"[bold cyan]workers[/] [{barcol}]{bar}[/] {busy}/{WORKERS} working",
              f"rate-limit: {lim_s}    deferred: {deferred}")
    t.add_row(f"sessions: {sessions}    watched: {watched} ch",
              f"today: {len(today)} runs    [bold]${spent:.2f}[/]")
    return Panel(t, title="🍡 Mochi_Bot", subtitle=time.strftime("%H:%M:%S"),
                 border_style="cyan", padding=(0, 1))


def agent_panel(d):
    now = time.time()
    phase = d.get("phase", "?")
    style = PHASE_STYLE.get(phase, "white")
    el = _dur(now - (d.get("start") or now))
    think = (d.get("thinking") or "").strip() or "…"
    head = Text.assemble(("● ", style), (phase, f"bold {style}"), ("   ", ""), (el, "dim"))
    meta = Text(f"{_short(d.get('server', '?'), 18)} · #{_short(d.get('channel', '?'), 18)} · "
                f"{_short(d.get('user', '?'), 14)}", style="dim cyan")
    body = Text(think[-360:], style="dim")
    return Panel(Group(head, meta, Text(""), body), border_style=style,
                 title=f"agent {d.get('pid', '?')}", width=56, padding=(0, 1))


def recent():
    u = _usage()
    t = Table(title="recent runs", expand=True, border_style="dim", title_style="bold dim")
    t.add_column("channel")
    t.add_column("cost", justify="right")
    t.add_column("turns", justify="right")
    t.add_column("dur", justify="right")
    for r in list(reversed(u))[:6]:
        t.add_row(_short(r.get("channel"), 22), f"${(r.get('cost_usd') or 0):.4f}",
                  str(r.get("turns") or "?"), f"{(r.get('duration_ms') or 0) // 1000}s")
    return t


def dashboard():
    runs = _runs()
    if runs:
        mid = Columns([agent_panel(d) for d in runs], expand=True)
    else:
        mid = Panel(Text("idle — no agents running right now", style="dim italic"),
                    border_style="dim", padding=(0, 1))
    return Group(overview(runs), mid, recent())


def main():
    with Live(console=Console(), screen=True, refresh_per_second=4) as live:
        try:
            while True:
                live.update(dashboard())
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
