---
name: activity-tracker
version: 1
script: infra/activity-tracker.sh
mutating: true
cron: "0 3 * * *"   # nightly Strava sync at 03:00 IST (also runs from briefing)
eval: skills/activity-tracker/eval.yaml
last_audited: 2026-05-08
description: |
  Workout / activity tracking. Strava-synced sessions sync nightly; manual
  entries (yoga, squash, swim, walk, etc. that Strava doesn't cover) go
  via `manual-log`. Stats query returns target progress for the year.
  WHEN: User reports an activity with explicit category + duration ("did
  30 min yoga today", "logged a 45 min HIIT session", "played squash for
  1 hour"); past-date entries; "activity stats" / "how many sessions
  this year" / "am I hitting my activity target".
  NOT WHEN: Generic exercise mentions without duration ("worked out") —
  ask. NOT for water (→ water-log) or daily tasks (→ daily-tasks).
trigger: |
  - "did 30 min yoga today" / "logged a 45 min HIIT session"
  - "played squash for 1 hour" / "30 min run on May 4"
  - "did yoga yesterday for 40 min"
  - "activity stats" / "how many sessions this year"
  - "am I hitting my activity target"
writes_to:
  - habits/activity-tracker
brain_dirs_read:
  - habits/activity-tracker
---

# /activity-tracker

Single source of truth: `infra/activity-tracker.sh` (deployed to `~/bin/`).
Strava sync wipes `activities.tsv` nightly; manual entries persist in a
separate `manual.tsv` so they survive sync.

## How to invoke

```bash
~/bin/activity-tracker.sh manual-log <category> <duration_min> "<title>"
~/bin/activity-tracker.sh manual-log <category> <duration_min> "<title>" <YYYY-MM-DD>
~/bin/activity-tracker.sh stats
~/bin/activity-tracker.sh sync           # cron-fired nightly
~/bin/activity-tracker.sh verify         # reconcile counters
```

Categories: `gym`, `running`, `tennis`, `others`.

## Resolver patterns

| User intent | Mapping | Command |
|---|---|---|
| "did 30 min yoga" / "HIIT" / "weights" / "strength" / "pilates" | category=`gym` | `manual-log gym 30 "yoga"` |
| "5km run" / "jog" / "marathon prep" | category=`running` | `manual-log running 30 "5km run"` |
| "tennis" / "squash" / "padel" / "pickleball" / "badminton" | category=`tennis` | `manual-log tennis 60 "squash"` |
| "swim" / "hike" / "walk" / "cycle indoors" / anything else | category=`others` | `manual-log others 45 "evening walk"` |
| "activity stats" / "how many sessions this year" | — | `stats` |

Past-date: append `<YYYY-MM-DD>` after the title.

## Telegram reply

Relay the script's stdout verbatim. After `manual-log`, the script
appends to `manual.tsv` and rebuilds `habits/activity-tracker` brain
page so future queries find the entry.

## Architectural rules

- **Never write to Strava's `activities.tsv` directly** — nightly sync
  wipes it. Always use `manual-log` (writes to `manual.tsv`).
- **Never compute stats from memory** — call `stats` for fresh numbers.
- **Manually-reported activities are write events** — persist before
  acknowledging.

## Smoke

The script supports `--smoke` (added in Wave 2).
