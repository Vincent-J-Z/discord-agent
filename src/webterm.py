"""Web terminal — a real PTY exposed over WebSocket (the "escape hatch").

Gives the dashboard a full terminal in the browser: it spawns a login shell in
a PTY and bridges it to a WebSocket, so xterm.js on the page is a live terminal
inside the container — run monitor.py, tmux, subagent.py, anything. The page
(served by monitor_web) loads xterm.js from a CDN in YOUR browser, so nothing
needs to be vendored here.

This is a full shell in the bypassPermissions container, so it is HARD-GATED:
it refuses to start unless MONITOR_TOKEN is set, and every connection must
present it (?token=). Bind 0.0.0.0 on MONITOR_WS_PORT (default 8898); publish it
alongside the http port in compose. MONITOR_WS_PORT=0 disables.
"""
import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

import websockets
from dotenv import load_dotenv

load_dotenv(os.path.join(os.environ.get("DISCORD_AGENT_WORKSPACE", "/workspace"), ".env"))

TOKEN = os.environ.get("MONITOR_TOKEN", "").strip()
PORT = int(os.environ.get("MONITOR_WS_PORT", "8898"))
SHELL_CWD = os.environ.get("CLAUDE_CWD", "/app")


def _spawn():
    """Fork a PTY running an interactive login shell. Returns (pid, master_fd)."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(SHELL_CWD)
        except OSError:
            pass
        os.environ["TERM"] = "xterm-256color"
        os.execvp("bash", ["bash", "-l"])
        os._exit(1)
    return pid, fd


async def handle(ws):
    # Every connection must carry the token in the query string.
    path = getattr(getattr(ws, "request", None), "path", "") or ""
    if not TOKEN or f"token={TOKEN}" not in path:
        await ws.close(code=1008, reason="unauthorized")
        return

    pid, fd = _spawn()
    loop = asyncio.get_running_loop()
    print(f"[webterm] session opened (pid {pid})", flush=True)

    async def pty_to_ws():
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, fd, 8192)
                if not data:
                    break
                await ws.send(data)
        except Exception:
            pass

    async def ws_to_pty():
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    # control frames (resize) come as JSON; everything else is keystrokes
                    if msg.startswith("{"):
                        try:
                            j = json.loads(msg)
                            if j.get("t") == "resize":
                                ws_size = struct.pack("HHHH", int(j["rows"]), int(j["cols"]), 0, 0)
                                fcntl.ioctl(fd, termios.TIOCSWINSZ, ws_size)
                                continue
                        except Exception:
                            pass
                    os.write(fd, msg.encode())
                else:
                    os.write(fd, msg)
        except Exception:
            pass

    try:
        await asyncio.wait([asyncio.create_task(pty_to_ws()),
                            asyncio.create_task(ws_to_pty())],
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass
        print(f"[webterm] session closed (pid {pid})", flush=True)


async def main():
    async with websockets.serve(handle, "0.0.0.0", PORT, max_size=None):
        print(f"[webterm] serving ws on :{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    if not TOKEN or PORT == 0:
        print("[webterm] disabled (needs MONITOR_TOKEN and MONITOR_WS_PORT != 0)", flush=True)
        raise SystemExit(0)
    asyncio.run(main())
