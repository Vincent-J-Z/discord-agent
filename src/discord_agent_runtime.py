"""Run the Discord Claude bridge and periodic status updater in one process."""
import os
import shutil
import stat
import subprocess
import sys
import time

from dotenv import load_dotenv


ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get(
    "DISCORD_AGENT_WORKSPACE",
    os.path.expanduser("~/discordAgentWorkspace"),
)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)

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
    if SWEEP_INTERVAL > 0 and _last_sweep() == 0.0:
        _mark_sweep()  # don't sweep the instant we boot; first sweep one interval later
    sweep = None
    resume = None
    try:
        while bridge.poll() is None:
            if gateway.poll() is not None:
                print("[runtime] gateway exited; respawning", flush=True)
                gateway = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_gateway.py")])
            # Auto-resume: when the rate limit clears, answer the deferred queue.
            if (resume is None or resume.poll() is not None) and _resume_due():
                print("[runtime] rate limit cleared — draining deferred queue", flush=True)
                resume = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_resume.py")])
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
