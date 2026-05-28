---
name: reminders
version: 2
script: tools/arete-remind.mjs
mutating: true
cron: null
eval: skills/reminders/eval.yaml
last_audited: 2026-05-28
description: |
  One-shot scheduled reminders via Telegram. The user wants the bot to
  ping them at a future time about something. Supports relative ("in
  30m"), absolute ("at 5pm", "tomorrow morning"), and named-time defaults
  ("tonight" = 21:00 local, "morning" = 09:00 local).
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
mutates_external_state: true
---

# /reminder

Schedules a durable reminder in Postgres. The router's background poller
fires it at the scheduled instant by sending a Telegram message.

Single source of truth: `/opt/arete/tools/arete-remind.mjs` (on PATH inside the container).

## How to invoke

```bash
# Relative — combinations of h/m/s
arete-remind "30m" "<message>"
arete-remind "1h"  "<message>"
arete-remind "2h30m" "<message>"

# Absolute — ISO-8601 UTC. YOU resolve the user's local time against the
# tenant's timezone (from identity/USER.md) before invoking.
arete-remind "2026-05-29T03:30:00Z" "<message>"
```

## Resolver patterns

Read the user's timezone from `identity/USER.md` (e.g. "Asia/Kolkata") and
convert the user's intent into UTC.

| User intent | How to resolve | Command |
|---|---|---|
| "remind me about X in 30 min" | relative — pass through | `arete-remind "30m" "X"` |
| "remind me in 1h to Y" | relative — pass through | `arete-remind "1h" "Y"` |
| "remind me to X at 5pm" | resolve to today's local 17:00 if not passed, else tomorrow; convert to UTC | `arete-remind "<ISO-UTC>" "X"` |
| "remind me tomorrow morning to X" | default 09:00 local tomorrow → UTC | `arete-remind "<ISO-UTC>" "X"` |
| "remind me tonight to X" | default 21:00 local today → UTC | `arete-remind "<ISO-UTC>" "X"` |
| "remind me on May 10 to file taxes" | default 09:00 local on date → UTC | `arete-remind "<ISO-UTC>" "file taxes"` |

For absolute times: pick the date/time in the user's timezone, then convert
to UTC. Example: user is in `Asia/Kolkata` (UTC+5:30) and asks for "5pm
today" — that is `17:00 IST = 11:30 UTC` — pass `"<TODAY>T11:30:00Z"`.

## Telegram reply

After `arete-remind` returns, echo back something natural like:

> "Got it — I'll ping you at 5pm today about X."

The CLI's own confirmation line (`✓ Reminder set for <ISO> (UTC): ...`) is
plumbing — don't paste it verbatim to the user.

## Architectural rules

- **Never promise "I'll remind you" without calling the CLI** — reminders
  that aren't scheduled don't fire.
- **Never re-ask** to disambiguate clear relative times. If wording is
  clear, schedule it.
- **Never use the agent's own delayed-reply mechanism** to fake a reminder
  — always go through `arete-remind`. The router persists in Postgres and
  fires reliably even if the container hibernates.
- **The reminder text** the bot delivers IS the `<message>` you pass. Make
  it a complete sentence ("Time to file your taxes" not just "taxes") —
  the user reads it cold hours later.

## What changed in v2 (vs Alfred's v1)

Alfred used `systemd-run --user` (host timers). That doesn't work for
multi-tenant containers because containers hibernate and timers die with
them. v2 persists to a `reminders` table in shared Postgres; the router's
poller delivers via the central Telegram sender even if the originating
tenant's container is hibernated at fire time.
