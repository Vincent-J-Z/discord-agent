"""Run the Discord Claude bridge and periodic status updater in one process."""
import os
import shutil
import stat
import subprocess
import sys
import time

from dotenv import dotenv_values, load_dotenv


ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get(
    "DISCORD_AGENT_WORKSPACE",
    os.path.expanduser("~/discordAgentWorkspace"),
)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)


def _check_env_token():
    """Guardrail against editing the wrong .env. The operative file is
    WORKSPACE/.env; the repo-root .env (one level above this file) is NOT loaded.
    Log which token is actually live, and warn loudly if a stray repo-root .env
    carries a different token so a mistaken edit can't silently keep the old one."""
    tail = lambda t: ("…" + t[-6:]) if t else "(none)"
    live = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    print(f"[env] CLAUDE_CODE_OAUTH_TOKEN live: {tail(live)} "
          f"(operative: {os.path.join(WORKSPACE, '.env')})", flush=True)
    stray = os.path.join(os.path.dirname(ROOT), ".env")  # repo-root /app/.env — unused
    if os.path.exists(stray):
        sv = dotenv_values(stray).get("CLAUDE_CODE_OAUTH_TOKEN")
        if sv and sv != live:
            print(f"[env] WARNING: {stray} has a DIFFERENT token {tail(sv)} but is NOT "
                  f"loaded — edit {os.path.join(WORKSPACE, '.env')} instead; this file is "
                  f"ignored.", flush=True)


_check_env_token()

TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "60"))
# Proactive review cadence. Activity-driven with timed fallback (0 disables):
#  - hourly fallback: review at least every SWEEP_INTERVAL even if all quiet
#  - activity-driven: if a human posted since the last review AND at least
#    SWEEP_MIN_GAP has passed, review early (so busy servers get seen in minutes,
#    quiet ones fall back to hourly). The activity signal is written by the bridge.
SWEEP_INTERVAL = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "3600"))
SWEEP_MIN_GAP = int(os.environ.get("SWEEP_MIN_GAP_SECONDS", "600"))
LAST_SWEEP_FILE = os.path.join(WORKSPACE, ".last_sweep")
ACTIVITY_FILE = os.path.join(WORKSPACE, ".activity")
LIMITED_FILE = os.path.join(WORKSPACE, ".limited_until")
DEFERRED_DIR = os.path.join(WORKSPACE, ".deferred")
SLACK_DEFERRED_DIR = os.path.join(WORKSPACE, ".deferred_slack")
REPORTS_DIR = os.path.join(WORKSPACE, ".worker_reports")
# Early-probe cadence for the deferred queue (see _resume_probe_due()): the
# reset time in LIMITED_FILE is often a guess (parse_reset_epoch() falls back
# to now + LIMIT_DEFAULT_COOLDOWN when it can't parse Claude's own wording),
# and a guess that lands later than the real reset would otherwise leave the
# bot silently sitting on a cleared queue until the guess catches up.
RESUME_PROBE_INTERVAL = int(os.environ.get("RESUME_PROBE_INTERVAL", "300"))
RESUME_PROBE_FILE = os.path.join(WORKSPACE, ".resume_probe_at")


def _not_limited():
    try:
        with open(LIMITED_FILE) as f:
            return time.time() >= float(f.read().strip())
    except Exception:
        return True


def _reports_due():
    """A finished worker left a report for the dispatcher to narrate."""
    try:
        pending = any(n.endswith(".json") for n in os.listdir(REPORTS_DIR))
    except FileNotFoundError:
        return False
    return pending and _not_limited()


def _resume_due():
    """True when the rate limit has reset and there are queued requests to answer."""
    try:
        if not any(n.endswith(".json") for n in os.listdir(DEFERRED_DIR)):
            return False
    except FileNotFoundError:
        return False
    try:
        with open(LIMITED_FILE) as f:
            until = float(f.read().strip())
    except Exception:
        until = 0.0
    return time.time() >= until


def _last_probe():
    try:
        with open(RESUME_PROBE_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _mark_probe():
    with open(RESUME_PROBE_FILE, "w") as f:
        f.write(str(time.time()))


def _clear_probe():
    try:
        os.remove(RESUME_PROBE_FILE)
    except OSError:
        pass


def _resume_probe_due():
    """True when there's a queue waiting on a limited_until that HASN'T passed
    yet, but it's been at least RESUME_PROBE_INTERVAL since the last early
    retry. Lets discord_resume.py's `--probe` mode find out whether the
    guessed reset (see RESUME_PROBE_INTERVAL comment above) was wrong, without
    hammering Claude every TICK_SECONDS. Safe because the actual gate is the
    RateLimited exception from run_claude, not this timer — a wrong probe is
    silent (see discord_resume.py)."""
    try:
        if not any(n.endswith(".json") for n in os.listdir(DEFERRED_DIR)):
            _clear_probe()  # no backlog — reset so the NEXT backlog starts its own cadence fresh
            return False
    except FileNotFoundError:
        return False
    try:
        with open(LIMITED_FILE) as f:
            until = float(f.read().strip())
    except Exception:
        until = 0.0
    now = time.time()
    if now >= until:
        return False  # already past the guess — _resume_due() handles this normally
    last = _last_probe()
    if last == 0.0:
        _mark_probe()  # first sighting of this backlog — start the cadence, don't probe instantly
        return False
    return (now - last) >= RESUME_PROBE_INTERVAL


def _slack_resume_due():
    """Same check as _resume_due(), for the separate Slack deferred queue (the
    usage/rate-limit gate itself is shared across platforms via LIMITED_FILE)."""
    try:
        if not any(n.endswith(".json") for n in os.listdir(SLACK_DEFERRED_DIR)):
            return False
    except FileNotFoundError:
        return False
    try:
        with open(LIMITED_FILE) as f:
            until = float(f.read().strip())
    except Exception:
        until = 0.0
    return time.time() >= until


def _last_sweep():
    try:
        with open(LAST_SWEEP_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _mark_sweep():
    with open(LAST_SWEEP_FILE, "w") as f:
        f.write(str(time.time()))


def _activity_at():
    try:
        with open(ACTIVITY_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _sweep_due():
    """Single decision for the proactive review: hourly fallback OR activity-driven
    (a human posted since last review and the min-gap has elapsed)."""
    if SWEEP_INTERVAL <= 0:
        return False
    elapsed = time.time() - _last_sweep()
    if elapsed >= SWEEP_INTERVAL:
        return True
    return SWEEP_MIN_GAP > 0 and elapsed >= SWEEP_MIN_GAP and _activity_at() > _last_sweep()


def setup_ssh():
    """Materialize the persistent /workspace/.ssh into ~/.ssh with the strict
    perms ssh demands (it refuses keys that are group/world readable). The
    canonical copy lives in the workspace (survives container recreates); this
    re-creates the real home copy on every start."""
    src = os.path.join(WORKSPACE, ".ssh")
    if not os.path.isdir(src):
        return
    dst = os.path.expanduser("~/.ssh")
    os.makedirs(dst, exist_ok=True)
    os.chmod(dst, 0o700)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        if not os.path.isfile(s):
            continue
        d = os.path.join(dst, name)
        shutil.copyfile(s, d)
        # Private keys / config → 600; public keys can stay readable.
        os.chmod(d, 0o644 if name.endswith(".pub") else 0o600)
    print("[runtime] ssh config materialized into ~/.ssh", flush=True)


def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    try:
        setup_ssh()
    except Exception as exc:
        print(f"[runtime] ssh setup failed: {exc}", flush=True)
    seen = os.path.join(WORKSPACE, ".discord_seen")
    if not os.path.exists(seen) or os.path.getsize(seen) == 0:
        with open(seen, "w") as f:
            f.write("0\n")

    bridge = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_claude_bridge.py")])
    # Presence keeper: holds a Gateway connection so the bot shows ONLINE.
    # Supervised separately and respawned if it dies; its absence must never
    # take the bridge down.
    gateway = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_gateway.py")])
    # Slack bridge (Socket Mode) — only when both Slack tokens are configured.
    slack_on = bool(os.environ.get("SLACK_BOT_TOKEN", "").strip()
                    and os.environ.get("SLACK_APP_TOKEN", "").strip())
    slack = subprocess.Popen([sys.executable, os.path.join(ROOT, "slack_bridge.py")]) if slack_on else None
    if slack_on:
        print("[runtime] slack bridge launched", flush=True)
    # Web monitor (dashboard). MONITOR_PORT=0 disables.
    webmon_on = os.environ.get("MONITOR_PORT", "8899").strip() != "0"
    webmon = subprocess.Popen([sys.executable, os.path.join(ROOT, "monitor_web.py")]) if webmon_on else None
    # Web terminal (PTY over WS) — only with a token set and a non-zero ws port.
    webterm_on = bool(os.environ.get("MONITOR_TOKEN", "").strip()) and \
        os.environ.get("MONITOR_WS_PORT", "8898").strip() != "0"
    webterm = subprocess.Popen([sys.executable, os.path.join(ROOT, "webterm.py")]) if webterm_on else None
    if SWEEP_INTERVAL > 0 and _last_sweep() == 0.0:
        _mark_sweep()  # don't sweep the instant we boot; first sweep one interval later
    sweep = None
    resume = None
    slack_resume = None
    report = None
    try:
        while bridge.poll() is None:
            if gateway.poll() is not None:
                print("[runtime] gateway exited; respawning", flush=True)
                gateway = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_gateway.py")])
            if slack_on and slack.poll() is not None:
                print("[runtime] slack bridge exited; respawning", flush=True)
                slack = subprocess.Popen([sys.executable, os.path.join(ROOT, "slack_bridge.py")])
            if webmon_on and webmon.poll() is not None:
                print("[runtime] web monitor exited; respawning", flush=True)
                webmon = subprocess.Popen([sys.executable, os.path.join(ROOT, "monitor_web.py")])
            if webterm_on and webterm.poll() is not None:
                print("[runtime] web terminal exited; respawning", flush=True)
                webterm = subprocess.Popen([sys.executable, os.path.join(ROOT, "webterm.py")])
            # Auto-resume: when the rate limit clears, answer the deferred queue.
            if resume is None or resume.poll() is not None:
                if _resume_due():
                    print("[runtime] rate limit cleared — draining deferred queue", flush=True)
                    resume = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_resume.py")])
                elif _resume_probe_due():
                    # Guessed reset time hasn't passed yet, but it's been a while —
                    # try anyway in case the guess is wrong. Silent if it isn't.
                    _mark_probe()
                    print("[runtime] probing deferred queue early (guessed reset may be wrong)", flush=True)
                    resume = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_resume.py"), "--probe"])
            # Same, for the separate Slack deferred queue.
            if slack_on and (slack_resume is None or slack_resume.poll() is not None) and _slack_resume_due():
                print("[runtime] rate limit cleared — draining slack deferred queue", flush=True)
                slack_resume = subprocess.Popen([sys.executable, os.path.join(ROOT, "slack_resume.py")])
            # A finished worker left a report — wake the dispatcher to narrate it.
            if (report is None or report.poll() is not None) and _reports_due():
                print("[runtime] worker report(s) pending — narrating", flush=True)
                report = subprocess.Popen([sys.executable, os.path.join(ROOT, "worker_report.py")])
            # Proactive review — activity-driven + hourly fallback, only if the
            # previous one has finished.
            if (sweep is None or sweep.poll() is not None) and _sweep_due():
                _mark_sweep()
                print("[runtime] launching proactive sweep", flush=True)
                sweep = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_sweep.py")])
            status_body = os.path.join(WORKSPACE, ".status.md")
            if os.path.exists(status_body):
                subprocess.run([sys.executable, os.path.join(ROOT, "discord_status.py")], check=False)
            time.sleep(TICK_SECONDS)
    finally:
        for proc in (gateway, bridge):
            if proc.poll() is None:
                proc.terminate()
    raise SystemExit(bridge.returncode or 0)


if __name__ == "__main__":
    main()
