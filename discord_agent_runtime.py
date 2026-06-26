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
    try:
        while bridge.poll() is None:
            if gateway.poll() is not None:
                print("[runtime] gateway exited; respawning", flush=True)
                gateway = subprocess.Popen([sys.executable, os.path.join(ROOT, "discord_gateway.py")])
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
