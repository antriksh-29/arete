---
name: habits
version: 1
script: null
mutating: true
cron: null
eval: skills/habits/eval.yaml
last_audited: 2026-05-16
description: |
  Habit-shaped messages dispatcher — detects quantity logs (water intake)
  and yes/no replies (reading check-in) and routes to the relevant log
  script. Compiled-view brain pages live under habits/.
  WHEN: "<n>ml" / "<n>L" patterns ("drank 750ml", "1.5L", "500 ml");
  reading check-in replies ("yes", "no", "didn't read", "read it");
  habit readback questions ("how much water today", "hydration so far",
  "did I read yesterday", "reading streak").
  NOT WHEN: water mentioned as context not log ("my car has 1.5L engine");
  ambiguous one-letter replies ("k", "ok") — ask once before logging.
trigger: |
  - quantity log: "<n>ml", "<n>L", "drank <n>ml", "<n>l water"
  - water readback: "how much water today", "hydration", "water intake"
  - reading log: "yes / yeah / yep / read it / done" (after check-in)
  - reading log: "no / nope / didn't read / skipped"
  - reading readback: "reading streak", "did I read yesterday"
writes_to: [habits]
brain_dirs_read: [habits]
mutates_external_state: true   # filesystem logs + brain page rewrites
---

# /habits — water intake + reading + future habits

When the user sends a habit-shaped message, detect the type and log it.

## Water intake

The water-log script supports three subcommands. Use the right one for each
intent:

### Logging (when user mentions a quantity)

Triggers: a number followed by "ml" or "L" (case-insensitive), with or
without a space. Examples: "750ml", "1.5L", "500 ml", "2l", "drank 1L",
"750ml water".

- Run: `~/bin/water-log.sh log "<amount>"` where `<amount>` is the quantity
  as stated (e.g. "750ml", "1.5L")
- Confirm briefly with the updated total: "Logged 750ml. Today: 2L / 3L"

Don't log if the message is clearly about something else (e.g. "my car has
1.5L engine"). Use context. If ambiguous, ask once.

### Readback (when user asks for the count)

Triggers: questions about water consumed, intake, hydration, etc.
Examples: "how much water have I drank today", "what's my water intake",
"am I hitting my hydration goal", "did I drink enough today".

- For today's running total in ml: `~/bin/water-log.sh total $(TZ=Asia/Kolkata date +%Y-%m-%d)`
- For a human-friendly summary (entries + total + target): `~/bin/water-log.sh summary $(TZ=Asia/Kolkata date +%Y-%m-%d)`
- For a past date: substitute the date argument, e.g. `... summary 2026-04-30`

Format the response naturally — pull the numbers from the script and reply
in plain language. Don't dump the raw script output; format it.

If the script returns 0 or empty, say "Nothing logged yet today" — don't
ask the user to log via "habit command" syntax. Logging just happens
naturally when they mention an amount.

### Storage (dual-layer)

- **Brain page** (compiled view): `~/.brain/habits/water-intake.md` —
  recomputed on every log call. Has todays entries, last 7 days table,
  and stats. This is what `gbrain__query` finds when user asks
  questions like "how am I doing on hydration" or "what habits am I tracking".
- **Filesystem** (raw events): `~/.brain-bootstrap-raw/state/water/YYYY-MM-DD.log` —
  one file per IST day, one line per entry. Used by the script for cheap
  total/summary computation. Day boundary 12:00am-11:59pm IST.

For a brain-first lookup question, you can ALSO call `gbrain__query "water intake"`
to get the compiled brain page back. The script readback (`total` /
`summary`) is faster for precise numeric questions; the brain page is
better for narrative "how am I doing" framing.

## Reading check-in replies

A daily reading check-in fires at 10am IST via the `reading-checkin.timer`
systemd unit. The Telegram message starts with "📚 Reading check-in".

When the user replies to that check-in (or sends an out-of-band update like
"yes I read", "didn't read", "yep", "skipped today"), recognize the answer
and log it:

- Affirmative ("yes", "yeah", "yep", "y", "read it", "yes did", "done"):
  run `~/bin/reading-checkin.sh log yes`
- Negative ("no", "nope", "n", "didn't read", "skipped"):
  run `~/bin/reading-checkin.sh log no`
- Specific date: pass it as the third arg, e.g.
  `~/bin/reading-checkin.sh log yes 2026-04-29`

After running, briefly confirm with the updated streak (e.g. "Logged. 6/7 last week.").

If the user message is ambiguous (just "k" or "ok"), ask one clarifying
question rather than guessing. Don't auto-log on unclear input.

## Adding a new habit

Follow the same pattern: brain page (`habits/<name>.md`), helper script
(`~/bin/<name>-log.sh`), AGENTS.md or this skill updated to recognize the
trigger pattern. See `skills/full-automation/SKILL.md` for the workflow.
