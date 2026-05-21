---
name: water-log
version: 0.1.0
script: infra/water-log.sh
mutating: true
cron: '0 8 * * *'
eval: user-skills/water-log/eval.yaml
last_audited: 2026-05-08
description: |
  Hydration tracking. The user logs water intake in ml/L, asks for today's
  total, asks for a daily / weekly / range summary, or backfills past days.
  WHEN: "drank 750ml", "had 500 ml of water", "logged 1L", "how much water
  today", "water total", "water summary", "weekly water review", "water
  since 1 May", "did I hit my water goal", or any explicit volume + water/
  glass/bottle term.
  NOT WHEN: "had a coffee" / "had a tea" with no ml stated — ask for the
  volume, do not auto-log. NOT for non-hydration intake (food, supplements).
trigger: |
  - "drank 750ml" / "had 500 ml of water" / "logged 1L" / "1.5L water"
  - "just had a glass" / "had a bottle" + explicit ml
  - "drank 2L yesterday" / "logged 750ml on May 4" (backfill)
  - "how much water today" / "water total" / "did I hit my water goal"
  - "water summary" / "water summary today" / "water summary yesterday"
  - "list today's water entries"
  - "water for the week" / "weekly water review" / "last 7 days water"
  - "water since 1 May" / "May 1–6 water" / "show me my water for the past 5 days"
writes_to:
  - habits/water-intake
brain_dirs_read:
  - habits/water-intake
created_by: alfred
source: telegram
forked_from: "skills/water-log@3878df1"
created_at: "2026-05-21T13:26:30.171Z"
last_authoring_turn: "2026-05-21T13:26:30.171Z"
---

# /water-log

Single source of truth: `infra/water-log.sh` (deployed to `~/bin/`). The
brain page `habits/water-intake` is auto-rebuilt on every mutation — never
read it directly to answer queries; always go through the script.

## How to invoke

```bash
# Log (today, current IST time)
~/bin/water-log.sh log "<amount>"            # 750ml | 0.5l | 1L

# Backfill (specific date)
~/bin/water-log.sh log "<amount>" <YYYY-MM-DD> [HH:MM]

# Read
~/bin/water-log.sh total <YYYY-MM-DD>        # ml number to stdout
~/bin/water-log.sh summary <YYYY-MM-DD>      # Telegram-formatted summary
~/bin/water-log.sh entries <YYYY-MM-DD>      # list of today's entries
~/bin/water-log.sh week                      # last 7 days roll-up
~/bin/water-log.sh range <from> <to>         # arbitrary inclusive range
```

For "today" use `$(TZ=Asia/Kolkata date +%Y-%m-%d)`.

## Resolver patterns

| User intent | Command |
|---|---|
| explicit volume + water/glass/bottle | `log "<amount>"` |
| coffee/tea with no ml stated | reply "How many ml?" — do NOT auto-log |
| past-date entry | `log "<amount>" <YYYY-MM-DD>` (with optional `HH:MM`) |
| "how much water today" / "water total" | `total $(TZ=Asia/Kolkata date +%Y-%m-%d)` |
| "water summary today" | `summary $(TZ=Asia/Kolkata date +%Y-%m-%d)` |
| "yesterday's water" | `summary <YYYY-MM-DD>` |
| "list today's water entries" | `entries $(TZ=Asia/Kolkata date +%Y-%m-%d)` |
| "water for the week" / "weekly review" | `week` |
| "water since X" / "May 1–6 water" / "past 5 days water" | `range <from> <to>` (inclusive) |

## Telegram reply

Relay the script's stdout verbatim (one-line ack like "✓ +750ml — 2.25L
today"). DO NOT append diagnostic information (entry count, page rebuild
status). Status → result, nothing else.

## Architectural rules

- **Never read `habits/water-intake` brain page directly** — it's a
  compiled view, not canonical state.
- **Never regurgitate** the previous reply when the user asks again —
  re-run the script every time. Logs change throughout the day.
- `summary` is stdout-only; the script never sends to Telegram from
  agent flows.
