---
name: skill-creator
description: Create a new reusable skill for yourself, or edit/remove an existing one, when the operator asks you to "add/create a skill", teach you a repeatable capability, or turn a workflow you just did into something reusable. Covers writing the SKILL.md, placing it, and persisting it via git so it survives restarts, rebuilds, and migrations.
---

# Creating (or editing) a skill

Skills live at `/app/.claude/skills/<name>/SKILL.md` and are auto-discovered —
each server working dir symlinks that folder in, so a new skill is live the
moment it's written (no restart). To add one:

1. **Pick a kebab-case name** (`weekly-report`, `pipeline-status`, not `task1`)
   and create the file `/app/.claude/skills/<name>/SKILL.md`.

2. **Write frontmatter + body.** The `description` is the TRIGGER — it decides
   when the skill fires, so make it specific and start with "when to use this":

   ```markdown
   ---
   name: <name>
   description: When the operator wants X … (one sentence; the trigger condition).
   ---

   # <what this does>

   The actual instructions — steps, exact commands, constraints, an example.
   Write it for a future run with NO memory of this conversation. Reference the
   toolbox by absolute path when used: /app/src/discord_api.py, slack_api.py,
   subagent.py. Keep it to ONE capability per skill; keep it short.
   ```

3. **Persist it — REQUIRED, or the work is only on this machine.** Commit and
   push using your self-maintenance flow (see CLAUDE.md):

       cd /app && git add .claude/skills && git commit -m "Add skill: <name>" \
         && git pull --rebase origin main && git push

   The folder is bind-mounted (survives container stop) *and* now in git
   (survives image rebuilds/migrations and reaches the operator's other
   checkout). If a rebase conflicts, STOP and tell the operator.

4. **Tell the operator**: the skill's name, what phrase/situation triggers it,
   and one concrete example of it in action.

## Editing / removing
- **Edit**: change the SKILL.md and push the same way (`Update skill: <name>`).
- **Remove**: delete the `<name>/` folder and push (`Remove skill: <name>`).

## Good skills
- One focused capability, not a grab-bag.
- A description that clearly says *when* to use it (that's the whole trigger).
- Self-contained instructions — don't assume context from the chat that created it.
- If it needs credentials/tools, name them and where they come from; don't invent.
