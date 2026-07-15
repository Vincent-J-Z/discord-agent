"""Drain the deferred-request queue once Claude's rate limit has reset.

Triggered by discord_agent_runtime in two ways:
  - normally, once the (guessed) limit window has passed — answers the queue
    oldest-first; stops if the limit is hit again.
  - as an early "--probe" retry (see discord_agent_runtime._resume_probe_due()):
    the guessed reset time from parse_reset_epoch() can land later than the
    real one, so we don't want the bot silently sitting on a cleared queue
    until the guess catches up. A probe skips the is_limited() gate below and
    lets drain_deferred(force=True) find out for real — if the limit hasn't
    actually cleared, run_claude raises RateLimited and the queue is put back
    untouched with nothing posted, so a wrong-but-early probe is invisible to
    users.
"""
import sys

import discord_claude_bridge as b


def main():
    probe = "--probe" in sys.argv
    if not probe and b.is_limited():
        return
    pending = b.list_deferred()
    if not pending:
        return
    if probe:
        print(f"[resume] probing early — {len(pending)} deferred request(s), "
              "guessed reset time may not have passed yet", flush=True)
    else:
        print(f"[resume] rate limit cleared — draining {len(pending)} deferred request(s)", flush=True)
    b.drain_deferred(force=probe)
    print("[resume] done", flush=True)


if __name__ == "__main__":
    main()
