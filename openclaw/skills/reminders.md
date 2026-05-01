---
name: reminders
version: 1.0.0
description: |
  Schedule one-shot reminders via systemd-run. Use when the user asks to be reminded at a specific time or after a delay.
---

# reminders — one-shot Telegram reminders

When the user asks to be reminded ("remind me in 30 min about X", "ping me
at 9pm to do Y", "nudge me tomorrow morning about Z"):

1. Parse the time spec into one of these forms:
   - Relative simple: `30m`, `1h`, `90s`, `2d`
   - Relative natural: `in 1h`, `in 30 min`, `in 2 hours`
   - Absolute IST (preferred for specific times): `@2026-05-02 09:30:00`
   - Absolute ISO 8601: `2026-05-02T09:30:00+05:30`

2. Call: `~/bin/schedule-reminder.sh "<when>" "<message>"`

3. The script returns the systemd unit name. Confirm to the user that it's
   scheduled with the absolute IST time, not vague language.

You MUST actually invoke the script using the exec/bash tool. Do not just
promise the reminder without running it. Do NOT use `gbrain__submit_job` for
reminders — that is for gbrain internal jobs, not shell scripts. The
schedule-reminder.sh script must be invoked via exec (bash).

Schedule status is auditable in `~/.brain-bootstrap-raw/state/reminders.jsonl`.

To send the user a one-off Telegram message at any time (not scheduled):
`~/bin/send-telegram.sh "message text"`. Use this for follow-ups inside
multi-step tasks, never for unsolicited messages.

