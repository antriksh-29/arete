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

## Data-flow contract (the invariants that keep this skill correct)

This skill has a single source of truth and a deterministic flow. If you
ever change the script, preserve every step here — `eval/fixtures/
reading-checkin/run-stats-eval.py` enforces the math via the gate.

### The chain

```
~/.brain/habits/book-reading.md  ← canonical state (Markdown table)
        ▲                                    │
        │                                    │ recompute_stats
        │ log_entry / verify                 ▼
   user reply                          ## Stats block
   "yes for 2026-05-10"                (yes/total/percent for 3 windows)
        │                                    │
        │                                    │ format_ping greps
        ▼                                    ▼
  reading-checkin.sh log         Telegram message body
        │
        │ commit_brain
        ▼
   git commit + git push origin main  (real-time, not daily)
```

### Invariants

1. **Brain page is canonical.** Stats are *derived* from the table on
   every read. Never cache stats anywhere. Never compute from memory.
2. **Every mutation rewrites the stats block.** `log_entry` calls
   `recompute_stats` after every insert / update — the page is never
   in a state where the table and stats disagree.
3. **Sort + dedupe + idempotency.** `log_entry` is idempotent on date:
   re-logging the same date with the same value produces no diff (no
   commit, no push). Re-logging with a *different* value updates in
   place. Sort runs on every mutation, so date order is preserved.
4. **Push is immediate.** `commit_brain` does both `git commit` AND
   `git push origin main`. GitHub reflects state in real time. Network
   failure during push logs a WARN but never blocks the user-facing
   reply (the next call retries; the daily collector at 7am IST is
   the safety net).
5. **3 stat windows, one source.** `last_7_days`, `this_month`, and
   `overall` are all derived from the same `entries` dict. They can
   diverge in count (different windows) but never in *which entries
   they consider* (always the brain page table).

### Stats math — exact specification

For a given `today` (IST date the script runs on):

| Window | Definition |
|---|---|
| `last_7_days` | entries `e` where `(today - e.date).days <= 7` AND `e.date <= yesterday` |
| `this_month` | entries `e` where `e.date >= today.replace(day=1)` AND `e.date <= yesterday` |
| `overall` | entries `e` where `e.date >= start_date` AND `e.date <= yesterday` |

`yesterday = today - 1 day`. The `e.date <= yesterday` filter excludes
today's entry from every window — today's entry doesn't exist yet
when the cron fires (the bot is asking the user to log today's
behaviour). After the user replies, the new entry slides into next
day's windows naturally.

For each window: `yes = sum where v == "yes"`, `total = len(window)`,
`percent = round(100 * yes / total)` (or 0 if empty).

### Tomorrow's flow walkthrough

To make the contract concrete, here's what happens at 10am IST May 11
*after the fixes in this commit*:

1. **`reading-checkin.timer`** fires `reading-checkin.service`.
2. Service runs `~/bin/reading-checkin.sh ping`.
3. `ping` calls `format_ping`:
   - `recompute_stats` reads the brain page (entries May 1 – May 9 + May 10 if you logged today, but if not yet then up to May 9).
   - Writes new `## Stats` block.
   - Greps the stats lines.
   - `format_ping` returns the message body.
4. `send-telegram.sh` ships the message to your chat.
5. Journal logs `OK: ping sent for 2026-05-10` (the `--for` arg or default).
6. `commit_brain` commits + **pushes** the rewritten page.
7. You reply `"yes"` or `"no"` (or explicit-date form).
8. OpenClaw routes to `~/bin/reading-checkin.sh log <yes|no>` (the script is in the exec allowlist now, no per-call prompt).
9. `log_entry` adds / updates the row, re-sorts, recomputes stats.
10. `commit_brain` commits + **pushes** again.
11. Bot replies "Logged yes for 2026-05-10."

The user-visible guarantee: **GitHub reflects every change within seconds, not at 7am the next day.**

## Smoke

The script supports `--smoke` (added in Wave 2) which prints a JSON
envelope and exits 0 without touching state files.
