---
name: habits
version: 1.0.0
description: |
  Habit-shaped messages (water intake, reading, exercise, etc.). Detect quantities or yes/no replies and route to the relevant log script.
---

# habits — water intake + reading + future habits

When the user sends a habit-shaped message, detect the type and log it.

## Water intake

Triggers: a number followed by "ml" or "L" (case-insensitive), with or
without a space. Examples: "750ml", "1.5L", "500 ml", "2l", "drank 1L",
"750ml water".

- Run: `~/bin/water-log.sh log "<amount>"` where `<amount>` is the quantity
  as stated (e.g. "750ml", "1.5L")
- The script returns the logged ml and today's running total
- Confirm briefly: e.g. "Logged 750ml. Today: 2.25L / 3L"

Don't log if the message is clearly about something else (e.g. "my car has
1.5L engine"). Use context. If ambiguous, ask once.

The day boundary is 12:00am–11:59pm IST (the script handles this automatically).

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

