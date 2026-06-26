---
name: discord
description: Read from and act on any Discord channel or thread in Mochi_Bot's guild — read messages, post/reply, react, edit, pin, list channels/threads, and create forum posts. Use whenever a request involves looking at or sending to Discord, especially another channel/thread (e.g. "reply to the @ in omega"), forums, reactions, or pins.
---

# Discord operations

Mochi_Bot acts over the Discord REST API with its bot token (`$DISCORD_BOT_TOKEN`).
All operations go through one CLI: **`/app/discord_api.py`**. Channel ids and
thread ids are interchangeable (a thread is just a channel).

## Recipes

Find where something is, then act:

```bash
python /app/discord_api.py channels          # text channels in the guild
python /app/discord_api.py threads           # active threads incl. forum posts
python /app/discord_api.py read <id> --limit 30
```

Respond:

```bash
python /app/discord_api.py reply <channel_id> <msg_id> "your reply"   # threaded reply
python /app/discord_api.py post  <channel_id> "message" --mention <user_id>
python /app/discord_api.py react <channel_id> <msg_id> ✅              # ack with emoji
python /app/discord_api.py edit  <channel_id> <msg_id> "updated text"  # edit own msg
python /app/discord_api.py pin   <channel_id> <msg_id>
```

Forums (e.g. `omega`) — a "new post" is a new thread:

```bash
python /app/discord_api.py forum-post <forum_id> "Post title" "first message body"
```

## Notes
- To reply inside an omega thread: `threads` to get the thread id, `read` it for
  context, then `reply`/`post` to that thread id.
- Never echo the token into a Discord message.
- Moderation, slash commands, and voice are NOT available via this toolbox —
  they need Developer-Portal permissions/intents or command registration. Say so
  instead of pretending.
