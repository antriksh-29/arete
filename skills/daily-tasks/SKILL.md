---
name: daily-tasks
version: 1
script: infra/daily-tasks.sh
mutating: true
cron: null
eval: skills/daily-tasks/eval.yaml
last_audited: 2026-05-08
description: |
  Today's task list. The user shares a fresh list, adds/removes/marks
  individual items, asks for the current list, asks for past lists, or
  replies to the 22:30 IST bulk-review prompt with a numbered status map.
  Single source of truth is the script's per-date state JSON; the brain
  page `habits/daily-tasks` is auto-rebuilt and is read-only for queries.
  WHEN: User shares a list of 2+ items framed as today's tasks; "add X
  to my list"; "remove #2"; "mark task 2 done"; "push #3 to tomorrow";
  "share today's task list" / "what's on my plate"; "tasks for May 4";
  bulk-review reply (`1. done / 2. tomorrow / ...`).
  NOT WHEN: User asks for a brain query about prior tasks across many
  days (→ query / brain-ops). NOT for one-off reminders (→ reminder).
trigger: |
  - "my tasks today are: ..." / "today's tasks: ..." / "to-do for today"
  - "tasks I'll do today" / "let me share my tasks"
  - "add X to my list" / "also add Y"
  - "remove #2" / "delete X from my list" / "drop the third one"
  - "I finished #1" / "done with X" / "mark task 2 done"
  - "push #2 to tomorrow" / "defer #3"
  - "park #2 indefinitely"
  - "share today's task list" / "my tasks" / "what's on my plate"
  - "what am I working on" / "today's tasks" / "today entire task list"
  - "yesterday's tasks" / "what did I do yesterday"
  - "tasks for 04 May" / "tasks for 2026-05-04" / "list from May 4"
  - "tasks 3 days ago"
  - bulk-review reply: numbered status map (1. done / 2. tomorrow / ...)
writes_to:
  - habits/daily-tasks
brain_dirs_read:
  - habits/daily-tasks
---

# /daily-tasks

Single source of truth: `infra/daily-tasks.sh` (deployed to `~/bin/`).
Per-date state JSON lives at `~/.brain-bootstrap-raw/state/daily-tasks/<YYYY-MM-DD>.json`.
The brain page `habits/daily-tasks` is auto-rebuilt on every mutation —
NEVER read it to answer queries; always go through the script.

## How to invoke

### Writes (always heredoc for set/add — tasks contain spaces)

```bash
# Replace today's list
~/bin/daily-tasks.sh set <<'EOF'
refactor signal detector
ship the briefing skill
reply to Bhramari
EOF

# Append to today's list
~/bin/daily-tasks.sh add <<'EOF'
review weekly metrics
EOF

# Remove (1-based index OR substring)
~/bin/daily-tasks.sh remove <index|text>

# Mark a status
~/bin/daily-tasks.sh mark <index> <yes|tomorrow|later|delete>

# Bulk review (22:30 IST prompt reply — pipe entire user reply)
~/bin/daily-tasks.sh review <<EOF
1. done
2. tomorrow
3. push indefinitely
EOF
```

### Reads

```bash
~/bin/daily-tasks.sh show                   # today
~/bin/daily-tasks.sh show yesterday
~/bin/daily-tasks.sh show <YYYY-MM-DD>
~/bin/daily-tasks.sh show -3                # 3 days ago
~/bin/daily-tasks.sh carryover              # used by morning briefing
```

## Resolver patterns

| User intent | Command |
|---|---|
| Shares list of 2+ items as today's tasks | `set` (heredoc) — REPLACES today |
| "add X to my list" / shares additional items after existing list | `add` (heredoc) — APPENDS |
| "remove #2" / "drop the third one" | `remove <index>` |
| "I finished #1" / "done with X" | `mark <index> yes` |
| "push #2 to tomorrow" / "defer #3" | `mark <index> tomorrow` |
| "park #2 indefinitely" / "later" | `mark <index> later` |
| Numbered status reply after 22:30 review prompt | `review` (heredoc, full reply) |
| "share today's task list" / "what's on my plate" | `show` |
| "yesterday's tasks" | `show yesterday` |
| "tasks for May 4" | `show 2026-05-04` (resolve to YYYY-MM-DD) |
| "tasks 3 days ago" | `show -3` |

After a mutation, reply with the script's one-line ack ("✓ Stored 4 tasks
for today"). Do NOT also dump the list back; the user can ask for it.

## Telegram reply

For `show` output: relay verbatim. Status emojis (✅ done, ➡️ tomorrow,
⏸️ later, 🗑️ deleted, • open) come from the script. Don't reformat.

## Architectural rules

- **NEVER regurgitate** from conversation history. Even if you replied
  to "share today's task list" five minutes ago, re-run `show` every
  time. The list changes on every mutation; cached answers are stale.
- **Never read `habits/daily-tasks` brain page directly** — that's a
  compiled view, not canonical state. Always go through `show`.
- **Never return yesterday's "Tomorrow" or "Indefinite" sections as
  today's list** — those are carryover hints for the morning briefing.
- **Never ask the user to clarify "set" vs "add"** when wording is clear
  ("my tasks today are X, Y, Z" → set; "add A" → add). Default to `set`
  only when the user explicitly frames a complete list.

## Smoke

The script supports `--smoke` (added in Wave 2) which validates argument
parsing without touching state files.
