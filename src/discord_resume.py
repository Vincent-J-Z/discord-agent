"""Drain the deferred-request queue once Claude's rate limit has reset.

Triggered by discord_agent_runtime when the limit window has passed and there are
queued requests. Answers them oldest-first; stops if the limit is hit again.
"""
import discord_claude_bridge as b


def main():
    if b.is_limited():
        return
    pending = b.list_deferred()
    if not pending:
        return
    print(f"[resume] rate limit cleared — draining {len(pending)} deferred request(s)", flush=True)
    b.drain_deferred()
    print("[resume] done", flush=True)


if __name__ == "__main__":
    main()
