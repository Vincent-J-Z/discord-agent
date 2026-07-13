"""Drain the Slack deferred-request queue once Claude's rate limit has reset.

Triggered by discord_agent_runtime when the limit window has passed and there
are queued Slack requests. Mirrors discord_resume.py for the Slack side.
"""
import discord_claude_bridge as b
import slack_bridge as s


def main():
    if b.is_limited():
        return
    pending = s.list_deferred_slack()
    if not pending:
        return
    print(f"[slack-resume] rate limit cleared — draining {len(pending)} deferred request(s)", flush=True)
    s.drain_deferred_slack()
    print("[slack-resume] done", flush=True)


if __name__ == "__main__":
    main()
