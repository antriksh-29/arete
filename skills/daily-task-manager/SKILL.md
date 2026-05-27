---
name: daily-task-manager
version: 1
script: infra/daily-tasks.sh
mutating: true
cron: null
eval: skills/daily-task-manager/eval.yaml
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
Per-date state JSON lives at `{{user.brain_path}}-bootstrap-raw/state/daily-tasks/<YYYY-MM-DD>.json`.
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

# Bulk review (22:00 IST prompt reply) — PREFER the --reply arg form.
# Inline arg survives mid-turn model failovers (OpenAI→Anthropic) which
# can drop stdin and silently apply zero updates (the May-13 outage).
~/bin/daily-tasks.sh review --date 2026-05-13 --reply "1. done
2. tomorrow
3. later
4. tomorrow
5. done
6. done"

# stdin form still works for backward compatibility, but is fragile:
~/bin/daily-tasks.sh review --date 2026-05-13 <<'EOF'
1. done
2. tomorrow
EOF
```

**MUST: always pass `--date <YYYY-MM-DD>` when handling a review reply.**
The 22:00 IST prompt message includes `for <YYYY-MM-DD>` (e.g.
`📋 Daily task review for 2026-05-13`). Extract that date and pass it
verbatim. If the user replies past midnight, this keeps the update on
the correct day's state file.

**MUST: always use `--reply "<text>"` for the review reply.** The
script will exit non-zero (code 2 or 3) if input is empty or contains
no parseable status lines — never silently claim a save. If the script
exits non-zero, relay the stderr verbatim. Do NOT compose a fake
"saved" reply.

**Why the HARD RULE exists — the May 9–11 outage.** The 22:00 IST evening
review is a systemd timer (`daily-tasks-review.timer`) that fires
`daily-tasks-review-trigger.sh`. The trigger ONLY sends the review
prompt if `{{user.brain_path}}-bootstrap-raw/state/daily-tasks/<today>.json` exists.
If the agent fabricates a "Saved. N tasks logged" reply WITHOUT invoking
the script, no state file is written → trigger exits silently → user
gets no reminder. That's the regression that produced the May 9–11
outage. Always invoke the script.

### Reads

```bash
~/bin/daily-tasks.sh show                   # today
~/bin/daily-tasks.sh show yesterday
~/bin/daily-tasks.sh show <YYYY-MM-DD>
~/bin/daily-tasks.sh show -3                # 3 days ago
~/bin/daily-tasks.sh carryover              # used by morning briefing

# v2 (cross-day) queries — backed by per-task lineage:
~/bin/daily-tasks.sh backlog                            # open tasks aged ≥7d, oldest first
~/bin/daily-tasks.sh backlog --age-gte 14               # tighter threshold
~/bin/daily-tasks.sh backlog --include-later            # include parked tasks
~/bin/daily-tasks.sh history 2026-05-04..2026-05-13     # span archive
~/bin/daily-tasks.sh stats --window week                # completion %, push count
~/bin/daily-tasks.sh stats --window month
~/bin/daily-tasks.sh task <uuid-prefix-or-title>        # full lineage for one task
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
| Reply to a carryover-collision prompt: "merge", "keep both", "skip" | Re-run last `set`/`add` with `--on-collision merge\|keep-both\|skip` |
| Numbered status reply after 22:00 review prompt | `review --date <YYYY-MM-DD> --reply "<text>"` |
| "share today's task list" / "what's on my plate" | `show` |
| "yesterday's tasks" | `show yesterday` |
| "tasks for May 4" | `show 2026-05-04` (resolve to YYYY-MM-DD) |
| "tasks 3 days ago" | `show -3` |
| "what's pending from my end" / "my backlog" / "stale tasks" | `backlog` |
| "what's been stuck for 2 weeks" | `backlog --age-gte 14` |
| "show me tasks from May 4 to May 13" | `history 2026-05-04..2026-05-13` |
| "how many tasks did I close this week" | `stats --window week` |
| "lineage of <task>" / "history of <task>" | `task "<title-substring>"` |

After a mutation, reply with the script's one-line ack ("✓ Stored 4 tasks
for today"). Do NOT also dump the list back; the user can ask for it.

## Telegram reply

For `show` output: relay verbatim. Status emojis (✅ done, ➡️ tomorrow,
⏸️ later, 🗑️ deleted, • open) come from the script. Don't reformat.

**HARD RULE — daily-task queries MUST invoke `~/bin/daily-tasks.sh`.**
This applies to ALL daily-task intents including the v2 commands
(`backlog`, `history`, `stats`, `task`). The script is the only
source of truth; the brain pages it auto-generates are read-only views.

DO NOT compose a daily-task reply from:
- chat context (the user might have just asked the same thing — re-run anyway)
- `gbrain query / search / get_page` against `habits/daily-tasks*` (those are stale views)
- your memory of what was on the list
- adjacent "what's pending in your life" framing that pulls from
  goals/projects pages (`originals/`, `entities/`, etc.)

If the user asks "what's pending" / "my backlog" / "stale tasks" /
"open tasks": run `~/bin/daily-tasks.sh backlog` and relay verbatim.
If they ask about a time span: run `~/bin/daily-tasks.sh history <range>`.
If they ask about completion / push rate / how many done this week:
run `~/bin/daily-tasks.sh stats --window week|month|all`.

Backlog reply starts with `📋 Backlog —`; stats reply starts with
`📊 Stats —`. If your reply doesn't start with one of these emoji
headers when the user asked a backlog/stats question, you didn't
invoke the script — abort and call it.

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
