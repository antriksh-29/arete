---
name: reading-checkin
version: 1
script: infra/reading-checkin.sh
mutating: true
cron: null
eval: skills/reading-checkin/eval.yaml
last_audited: 2026-05-08
description: |
  Reading habit check-in. The 10am IST cron sends "Did you read a book
  yesterday?" The user replies yes/no, asks for stats, asks for gap
  verification, or backfills.
  WHEN: yes/no replies that follow the 10am check-in ("yes", "y", "yep",
  "no", "nope", "skipped"); stats queries ("reading streak", "reading
  stats", "how am I doing on reading"); gap queries ("any gaps in
  reading log", "are all reading days logged"); backfill ("backfill
  reading for May 4").
  NOT WHEN: User says "I finished <book title>" — that's reading-list
  read-marking, NOT a daily yes/no log. NOT WHEN they're talking about
  reading articles, threads, or PDFs (→ reading-list / ingest-*).
trigger: |
  - "yes" / "y" / "yep" / "yeah" (after the 10am check-in)
  - "no" / "n" / "nope" / "skipped" / "didn't" (after the 10am check-in)
  - "yes for Sunday" / "no for 5 may" (specific past-date)
  - "what's my reading streak" / "reading stats" / "how am I doing on reading"
  - "are all reading days logged" / "any gaps in reading log"
  - "did anything get missed in the reading log"
  - "backfill reading for <date>"
writes_to:
  - habits/book-reading
brain_dirs_read:
  - habits/book-reading
---

# /reading-checkin

Single source of truth: `infra/reading-checkin.sh` (deployed to `~/bin/`).
The cron at 10am IST pings the user; their yes/no reply is a write event
that MUST be persisted via the script before the agent acknowledges.

## How to invoke

```bash
# Log a result (defaults to yesterday IST)
~/bin/reading-checkin.sh log <yes|no>

# Log for a specific past date
~/bin/reading-checkin.sh log <yes|no> <YYYY-MM-DD>

# Stats (current streak, 7d / 30d windows)
~/bin/reading-checkin.sh stats

# Verify the log has no gaps (lists missing dates; exit 2 if gaps found)
~/bin/reading-checkin.sh verify
```

## Resolver patterns

| User reply | Command |
|---|---|
| "yes" / "y" / "yep" / "yeah" / "done" | `log yes` |
| "no" / "n" / "nope" / "skipped" / "didn't" | `log no` |
| "yes for Sunday" / "no for 5 may" | `log <yes|no> <YYYY-MM-DD>` |
| "what's my reading streak" / "reading stats" | `stats` |
| "any gaps in reading log" / "anything get missed" | `verify` |
| "backfill reading for May 4" | `log <yes|no> 2026-05-04` |

## Telegram reply

After `log` returns, reply with a short ack — the script's stdout already
contains the streak update ("✓ Logged. Reading streak: 7 days"). Relay
verbatim. DO NOT just say "noted".

After `verify`, surface the missing dates list directly. If exit 2 (gaps
exist), prompt the user to backfill.

## Architectural rules

- **Yes/no after the 10am check-in is ALWAYS a write event.** Persist via
  the script before any acknowledgement. Never just say "noted".
- **Never compute stats from memory.** Always call `stats` for fresh
  numbers. The math is in the script.
- **The cron handles the ping.** Never re-fire the check-in from the
  agent flow.

## Smoke

The script supports `--smoke` (added in Wave 2) which prints a JSON
envelope and exits 0 without touching state files.
