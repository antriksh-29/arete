---
name: reminder
version: 1
script: infra/schedule-reminder.sh
mutating: true
cron: null
eval: skills/reminder/eval.yaml
last_audited: 2026-05-08
description: |
  One-shot scheduled reminders via Telegram. The user wants the bot to
  ping them at a future time about something. Supports relative ("in
  30m"), absolute ("at 5pm", "tomorrow morning"), and named-time defaults
  ("tonight" = 21:00 IST, "morning" = 09:00 IST).
  WHEN: "remind me to X at <time>", "ping me at <time> about Y", "remind
  me about X in <duration>", "remind me tomorrow morning to X", "remind
  me on <date> to X", "remind me tonight to X".
  NOT WHEN: Recurring schedules (those go to cron-scheduler skill). NOT
  for "remind me about X" with no time anchor — ask for the time.
trigger: |
  - "remind me to X at 5pm" / "ping me at 17:30 about Y"
  - "remind me about X in 30 min" / "ping me in 1h to Y"
  - "remind me tomorrow morning to X" / "remind me tomorrow to X"
  - "remind me on May 10 to X" / "remind me next Monday about X"
  - "remind me tonight to X"
writes_to: []
brain_dirs_read: []
mutates_external_state: true   # systemd-run user units, not brain
---

# /reminder

Single source of truth: `infra/schedule-reminder.sh` (deployed to `~/bin/`).
Backed by `systemd-run --user` — each reminder gets a unique unit name
and fires Telegram at the scheduled IST time.

## How to invoke

```bash
# Relative (script accepts: 30m, 1h, 2h30m, etc.)
~/bin/schedule-reminder.sh "30m" "<message>"
~/bin/schedule-reminder.sh "1h"  "<message>"

# Absolute (always IST; agent must resolve "tomorrow", "next Monday", "tonight")
~/bin/schedule-reminder.sh "@<YYYY-MM-DD> HH:MM:SS" "<message>"
```

## Resolver patterns

| User intent | How to resolve | Command |
|---|---|---|
| "remind me to X at 5pm" | resolve to today IST if 17:00 hasn't passed, else tomorrow | `"@<YYYY-MM-DD> 17:00:00" "X"` |
| "remind me at 17:30 about Y" | same logic | `"@<YYYY-MM-DD> 17:30:00" "Y"` |
| "remind me about X in 30 min" | use relative form | `"30m" "X"` |
| "remind me in 1h to Y" | use relative form | `"1h" "Y"` |
| "remind me tomorrow morning to X" | default 09:00 IST | `"@<tomorrow> 09:00:00" "X"` |
| "remind me on May 10 to file taxes" | default 09:00 IST unless time given | `"@2026-05-10 09:00:00" "file taxes"` |
| "remind me tonight to X" | default 21:00 IST | `"@<today> 21:00:00" "X"` |

Always resolve relative dates to absolute YYYY-MM-DD against IST before
invoking the script.

## Telegram reply

After the script returns, reply with the script's confirmation
(e.g. "✓ Reminder set for 2026-05-08 09:00 IST: file taxes"). Relay
verbatim.

## Architectural rules

- **Never promise "I'll remind you" without calling the script** —
  reminders that aren't scheduled don't fire.
- **Never re-ask** the user to disambiguate a clear relative time
  ("did you mean in 30 min or at 30 past?"). If wording is clear,
  schedule it.
- **Never use the agent's own delayed-reply mechanism** to fake a
  reminder — always go through systemd-run.

## Smoke

The script supports `--smoke` (added in Wave 2) which validates time
parsing without scheduling a unit.
