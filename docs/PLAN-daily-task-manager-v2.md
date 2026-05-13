# Plan — daily-task-manager v2

Status: draft for `/plan-eng-review`.
Date: 2026-05-13.
Owner: antriksh-29.

## Problem statement

The daily-task-manager skill captures a flat per-date list of tasks
(`~/.brain-bootstrap-raw/state/daily-tasks/<YYYY-MM-DD>.json`) and
fires a 22:00 IST review prompt that asks the user to mark each task
done / tomorrow / later / skip. As of 2026-05-13 this is broken in
two ways, and structurally not capable of the longer-term workflow
the user actually wants.

### What broke last night (2026-05-13)

Evidence: screenshots IMG_9872-v1 / IMG_9873-v1, gateway log file
`/tmp/openclaw-1000/openclaw-2026-05-13.log`, on-disk state file
`~/.brain-bootstrap-raw/state/daily-tasks/2026-05-13.json`.

| # | Symptom | Root cause |
|---|---------|------------|
| B1 | Bot replied "✓ Applied 0 status updates to 2026-05-13" despite user's 6-line numbered status reply. | Bot invoked `~/bin/daily-tasks.sh review` but the user's reply was not piped into stdin (likely dropped during an mid-turn OpenAI→Anthropic failover). Parser saw zero input → zero updates. |
| B2 | "What's the final status of each task for today?" returned all 6 tasks as "pending" with the script's plain task list — the bot fabricated the "— pending" suffix. | `daily-tasks.sh show` only renders the task list, not statuses. Bot hallucinates the suffix to look responsive. |
| B3 | After the bungled review, state file still has `statuses: {}`. User has no record of what they marked. | Direct consequence of B1. No replay path. |

### What was never built

The current script is per-date-flat. It cannot answer:

- *"What's still pending from earlier this week?"*
- *"Is the task I pushed to tomorrow on 13 May now closed?"*
- *"How many tasks have been sitting in my backlog longer than 7 days?"*

Today's "Today" list and yesterday's "Tomorrow" list are not linked.
Each `set` replaces the day's list with no awareness of carryover.
The brain page `habits/daily-tasks` is a one-day snapshot, not a
durable record.

## Desired capabilities (from user, 2026-05-13)

1. Share tasks at any time of day → bot persists them via the script.
2. 22:00 IST reminder asks for status per task. Statuses get applied.
3. Tasks marked **tomorrow** flow into the next day's list and
   surface in the 08:00 morning briefing.
4. A task that is **tomorrow** on 13 May and **done** on 14 May has
   final status "done" — the lineage collapses to a single logical
   task.
5. A task that has been **tomorrow** repeatedly and is still not
   done after N days (default 7) ages into a **backlog** bucket.
6. Backlog is queryable from Telegram: *"what's pending from my end
   till date"*.
7. Backlog is queryable from the brain so it's accessible to any
   workflow (morning briefing, weekly review, dashboard).
8. The system should compound: short-term it reduces overhead,
   long-term it surfaces patterns (always-pushing-X, completion
   rate, average time-to-done).

## Proposed solution

### Data model — task-centric, not date-centric

Move from a flat per-date file to a per-task lineage. Each logical
task gets a UUID. Per-date files become thin pointers.

```
~/.brain-bootstrap-raw/state/daily-tasks/
  tasks/
    <uuid>.json        # one file per logical task lineage
  daily/
    2026-05-12.json    # { date, task_ids[] } — assignments for that day
  index.json           # { open_tasks: [uuids], by_title_norm: {...} }
```

`tasks/<uuid>.json`:

```json
{
  "id": "ce7d...",
  "title": "Ship browser and booking flow end to end",
  "title_norm": "ship browser and booking flow end to end",
  "created_date": "2026-05-12",
  "created_at": "2026-05-12T13:01:50+05:30",
  "events": [
    {"date": "2026-05-12", "status": "tomorrow", "ts": "2026-05-12T22:00:00+05:30"},
    {"date": "2026-05-13", "status": "yes",      "ts": "2026-05-13T22:49:00+05:30"}
  ],
  "current_status": "yes",
  "closed_at": "2026-05-13T22:49:00+05:30",
  "tags": []
}
```

`daily/2026-05-12.json`:

```json
{
  "date": "2026-05-12",
  "task_ids": ["ce7d...", "9b4a...", "..."]
}
```

`index.json` is a derived view rebuilt by the script on every
mutation. It exists so backlog and stat queries do not have to scan
all task files on every read.

### Status machine

| Status     | Terminal? | Carry forward to next day? | Lands in backlog? |
|------------|-----------|----------------------------|-------------------|
| `open`     | no        | no (today only)            | no (yet)          |
| `yes`      | yes       | no                         | no                |
| `tomorrow` | no        | yes (next day)             | no, but tracked   |
| `later`    | no        | no (parked, no auto-carry) | yes               |
| `delete`   | yes       | no                         | no                |

A task is "in backlog" if `current_status ∈ {tomorrow, later}` AND
`days_since_created >= BACKLOG_THRESHOLD` (default 7).

A task is "open" if `current_status ∈ {open, tomorrow, later}`.

### Carryover rule

When `set` runs for date D and there exists D-1 with tasks marked
`tomorrow`: those tasks are auto-prepended to today's task_ids
(not duplicated; same UUIDs reused). A `set` does NOT replace
carried-over tasks unless they appear in the new list with explicit
delete intent.

This means: user marks `tomorrow` last night → user shares "today's
tasks: X, Y, Z" this morning → the resulting list is `[<carryovers>,
X, Y, Z]` and the morning briefing shows the carryovers prominently.

### New CLI surface

Backward-compatible:

```bash
~/bin/daily-tasks.sh set       # heredoc, same as today
~/bin/daily-tasks.sh add       # heredoc, same
~/bin/daily-tasks.sh remove    # by index/text, same
~/bin/daily-tasks.sh mark <idx> <status>   # same
~/bin/daily-tasks.sh show [date]           # NOW includes per-task status emoji
~/bin/daily-tasks.sh review                # parses numbered reply, applies statuses
```

New:

```bash
~/bin/daily-tasks.sh review --reply "<inline text>"   # B1 fix: accept reply as arg too
~/bin/daily-tasks.sh backlog                          # all open tasks across dates
~/bin/daily-tasks.sh backlog --age-gte 7              # filter by age
~/bin/daily-tasks.sh history <YYYY-MM-DD..YYYY-MM-DD> # span query
~/bin/daily-tasks.sh stats [--week|--month]           # completion rate, push count, etc.
~/bin/daily-tasks.sh task <uuid-prefix>               # detail view + lineage
~/bin/daily-tasks.sh rebuild-index                    # idempotent index rebuild
```

### Brain pages (gbrain-accessible)

Auto-rebuilt on every mutation:

| Page                          | Content                                        |
|-------------------------------|------------------------------------------------|
| `habits/daily-tasks`          | Today's list + statuses, last 7 days summary.  |
| `habits/daily-tasks-backlog`  | Open tasks aged ≥ 7 days, sorted oldest first. |
| `habits/daily-tasks-history`  | Reverse-chronological day-by-day archive.      |
| `habits/daily-tasks-stats`    | Weekly + monthly completion / push / drop %.   |

### Daily flow (post-v2)

1. User shares tasks any time → bot calls `set` → tasks created with
   `open` status, today's `daily/<date>.json` references them.
2. Morning briefing (08:00 IST cron) reads `carryover` from
   yesterday's `tomorrow`-tagged tasks + today's already-set tasks,
   sends combined list.
3. User adds / removes / marks throughout the day → events appended
   to per-task lineage.
4. Evening review (22:00 IST cron) sends review prompt with current
   open tasks for today. User replies with numbered statuses.
5. Bot calls `review --reply "<reply text>"` (the new arg form) so
   stdin-drop bugs can't kill the update.
6. Statuses applied → tasks transitioning to `tomorrow` will appear
   in tomorrow's morning briefing; tasks to `later` go to backlog;
   tasks to `yes` close cleanly; tasks not replied stay `open`.
7. Backlog aging runs nightly (could piggyback on the 22:00 timer
   or a separate 00:05 cron).

### Fix for B1 specifically

Two-layer defense:

a. **`review --reply "<text>"` arg form** (preferred path) — bot
   passes the user's reply directly as an argument; no stdin
   ambiguity. AGENTS.md hard rule updated to use the arg form.

b. **Fallback wrapper** `~/bin/daily-tasks-review-apply.sh` that
   accepts arbitrary input via stdin OR `$1` and shells out to
   `daily-tasks.sh review` with the right plumbing. This is a
   belt-and-braces guard against model-handoff edge cases.

### Fix for B2 specifically

`daily-tasks.sh show` updated to emit:

```
📋 Today — 6 tasks (2 done · 1 tomorrow · 1 later · 2 open)

✅ 1. check daily tasks review cron
➡️ 2. understand from cursor entire daily tasks review flow
⏸️ 3. pause browser automation via browser-harness
✅ 4. stress test form filling workflows
• 5. read jobs book
• 6. dinner meeting — go prepared with what to figure out
```

The bot then relays this verbatim (existing hard rule). No more
hallucinated "— pending" suffixes.

### Migration of historical data

We have 7 historical state files (`2026-05-04` through `2026-05-08`,
plus `2026-05-12` and `2026-05-13`). Migration script:

1. Walk each file in chronological order.
2. For each task title, normalize (lowercase, strip punctuation) and
   try to fuzzy-match against the existing in-memory task index using
   token-set ratio ≥ 0.85.
3. Match → append a new event to the existing task lineage.
4. No match → create a new task UUID.
5. Apply known statuses from the old `statuses` dict.
6. Write the new task-centric files; archive originals as `.v1.bak`.
7. Rebuild `index.json`.

Migration is idempotent and dry-run-capable (`--dry-run` prints the
mapping table without writing).

### Telegram review UX upgrade (v2.1, deferred)

Numbered status replies are friction. v2.1 will use Telegram inline
keyboard buttons (`[done] [tomorrow] [later] [delete]` per task)
so a single tap registers status. Out of scope for this plan.

## Eng-review decisions (locked 2026-05-13)

Locked via `/plan-eng-review` + outside-voice Claude subagent.

| # | Decision | Rationale |
|---|----------|-----------|
| A1 | Arg-form `--reply "<text>"` only. NO wrapper script. Routing rule lives in `skills/daily-task-manager/SKILL.md`, NOT in `identity/AGENTS.md`. | Thin harness, fat skills. DRY. One debug surface. |
| A2 | Python rewrite (`infra/daily-tasks.py`) with thin bash wrapper at the existing `~/bin/daily-tasks.sh` path. Cron + SKILL.md references unchanged. | Bash + jq for relational queries is exactly the shape that bit us. Python gives proper argparse, exit codes, structured tests. |
| A3 | Migration: `--dry-run` prints mapping table → user reviews manually → explicit `--apply` writes. NO auto-apply. | 7 historical files is fast to eyeball. Silent collapse from fuzzy match is a real risk. |
| A4 | Index rebuild on every mutation. Brain page rebuilds **debounced 5s** (avoids 10×-per-review churn during the 22:00 status reply). | Low volume; no stale-read risk for queries. Debounce keeps brain-dir git-commits sane. |
| C2 | Phase 1 ships BOTH a unit test (`review` with empty stdin AND empty args exits non-zero) AND a Telethon E2E (`tasks-set-and-review`). | Exact regression test for last night's bug. |
| C3 | Phase 1 today; Phase 2-5 within 3 days. | Tonight's review unblocked; redesign follows quickly. |
| O1 | **Lineage matching during daily input.** On `set`/`add`, normalize each new title and search the open-task index for a title-norm match. If found, reuse the existing UUID and append an event; do NOT create a new task. | Without this, the per-task model is just prettier storage. Same algorithm covers carryover-dedupe and migration-day-merge. |
| O2 | Carryover-dedupe: when `set` runs and an incoming title matches a just-carried `tomorrow` task, **bot prompts** user: "I already carried over `<title>` from yesterday. Replace or keep both?" | Explicit beats silent. Friction is small (one prompt per ambiguity) and only fires on collision. |
| O4 | Late-reply date resolution: `review --reply` applies to the **date in the prompt message** (encoded into the trigger script's send: `📋 Daily task review for 2026-05-13`). If user replies at 02:00 next day, it still goes to 2026-05-13's state file. | Deterministic. Survives time-zone drift and the 22:00 → next-day boundary. |
| O7 | Telethon E2E test moves from Phase 5 → Phase 1. | Ship the regression test with the fix. |

Outside-voice findings handled without separate AskUserQuestion:

- **`later` semantics**: `later` = "parked indefinitely". Does NOT auto-carry. Shows ONLY in `backlog --include-later`. No auto-resurface; user must explicitly ask for parked tasks. This makes the status semantically distinct from `tomorrow`.
- **Phase 3-5 decoupling from Phase 2 migration gate**: backlog/stats can run via a read-only adapter over the v1 flat files until migration applies. Phases 3-5 are not blocked by manual migration review.
- **Migration rollback**: `migrate-daily-tasks-v2.py --apply` archives v1 files to `~/.brain-bootstrap-raw/state/daily-tasks/.v1-backup-<timestamp>/`. A `--rollback <timestamp>` flag restores them and removes v2 artefacts. Solo user + no staging = mandatory escape hatch.

## Implementation phases (eng-review-locked)

### Phase 1 — tactical fix (today, 2026-05-13, blocking)

Goal: tonight's review at 22:00 IST works correctly. Backlog query
not yet available but evening review applies statuses.

- Add `review --reply "<text>"` arg form to existing
  `daily-tasks.sh` (no data model change yet).
- `daily-tasks.sh review` exits non-zero if both stdin AND `--reply`
  arg are empty/absent (the B1 regression guard).
- `show` upgraded to include status emoji + summary line.
- Trigger script (`daily-tasks-review-trigger.sh`) prepends the prompt
  date: `📋 Daily task review for <YYYY-MM-DD>` so the bot can extract
  it and pass `--date <YYYY-MM-DD>` to `review` (O4 deterministic
  late-reply handling).
- Re-apply the 2026-05-13 lost statuses (from the screenshot) directly
  via `daily-tasks.sh review --date 2026-05-13 --reply "1.done 2.tomorrow 3.later 4.tomorrow 5.done 6.done"`.
- `skills/daily-task-manager/SKILL.md` updated with the `--reply` arg
  form + the date-from-prompt extraction rule. (NOT in AGENTS.md.)
- Unit test: empty stdin AND empty `--reply` → exit non-zero with
  clear stderr.
- Telethon E2E: `tasks-set-and-review` — set 3 tasks via Telegram,
  fire review prompt via trigger script directly (not waiting for
  22:00), send numbered reply, assert state file `statuses{}` has
  3 entries.

Estimated: 3 hours.

### Phase 2 — v2 data model (1–2 days)

- New `infra/daily-tasks.py` (Python rewrite). `~/bin/daily-tasks.sh`
  becomes a 3-line wrapper invoking `daily-tasks.py "$@"`. Cron +
  SKILL.md + AGENTS.md references unchanged.
- Per-task lineage files (`tasks/<uuid>.json`), per-date pointer
  files (`daily/<YYYY-MM-DD>.json`), derived `index.json` rebuilt on
  every mutation.
- Title-normalization + open-task lookup for cross-day lineage
  (O1 algorithm).
- Carryover logic in `set` with dedupe prompt (O2).
- Migration script `infra/migrate-daily-tasks-v2.py`:
  - `--dry-run` prints mapping table (one row per source task → target
    UUID, with fuzzy-match confidence) to stdout. No writes.
  - `--apply` writes new files and archives v1 to `.v1-backup-<ts>/`.
  - `--rollback <ts>` restores from a named backup.
  - Fixture-based unit test using the 7 historical files.
- Existing CLI shape preserved (set/add/remove/mark/show/carryover)
  but reimplemented atop the new model.

Estimated: 1.5 days.

### Phase 3 — brain integration + read-only adapter (0.5 day)

- Auto-rebuild four brain pages: `habits/daily-tasks`,
  `habits/daily-tasks-backlog`, `habits/daily-tasks-history`,
  `habits/daily-tasks-stats`. Debounced 5s on mutations.
- gbrain pin so `gbrain search "pending tasks"` returns
  `habits/daily-tasks-backlog`.
- Read-only adapter so Phase 3 lands BEFORE Phase 2 migration is
  applied: backlog/stats run over the existing v1 flat files until
  migration completes. Decouples this phase's ship from manual
  migration review.

Estimated: 4 hours.

### Phase 4 — morning briefing wiring (0.5 day)

- Daily briefing skill reads `carryover` + today's `daily/<date>.json`
  and renders the combined list in the 08:00 ping.
- Backlog older than 14 days surfaces as a "stuck tasks" callout.

Estimated: 4 hours.

### Phase 5 — E2E coverage completion (0.5 day)

Note: the `tasks-set-and-review` E2E case is shipped in Phase 1.
This phase adds the remaining three:

- Telethon harness adds 3 more test cases:
  - `tasks-carryover-tomorrow`: mark 1 tomorrow, simulate next-day
    `set` with a colliding title, assert dedupe prompt fires.
  - `tasks-backlog-query`: "what's pending from my end" → assert
    reply contains the right open tasks (aged ≥7d).
  - `tasks-stats-week`: "how many tasks did I close this week" →
    asserts a numeric response from the script.

Estimated: 4 hours.

## Risks + tradeoffs

| Risk | Mitigation |
|------|------------|
| Migration mis-matches fuzzy-matched titles, collapsing two distinct tasks into one lineage. | `--dry-run` mapping table reviewed manually before apply; archive originals. |
| The v2 model breaks the existing `cron-scheduler` morning-briefing flow. | Phase 2 keeps `show` CLI shape identical; Phase 4 is the only briefing-touching phase and is feature-flagged. |
| Bot still drops stdin on review — even arg form fails. | Fallback wrapper script `daily-tasks-review-apply.sh` shells out to both arg + stdin paths. Both must drop for the update to fail. |
| Telegram review UX still requires numbered free-text reply, prone to model paraphrasing. | v2.1 will move to inline keyboard buttons. Not in this plan. |
| Index.json grows unbounded as historical tasks accumulate. | Index keeps only open + last-90-days closed. Closed-old tasks live in per-task files only; queried via `history`. |

## Out of scope

- Inline keyboard review UX (v2.1).
- Task-level due dates beyond "tomorrow" / "later" (e.g. "remind me
  on Friday").
- Tags / projects.
- Multi-user / sharing.

## Open questions for the user

1. **Backlog age threshold** — proposed 7 days for "ages into
   backlog", 14 days for "stuck task callout". OK or different?
2. **Carryover behaviour** — if you say "my tasks today are X, Y,
   Z" after carrying over `tomorrow` task A, should the resulting
   list be `[A, X, Y, Z]` (auto-prepend) or just `[X, Y, Z]` (your
   `set` is canonical)?
3. **Migration scope** — backfill all 7 historical files into the
   new model? (Recommended yes for a clean stats baseline.)
4. **Phase 1 vs full plan** — ship Phase 1 today for tonight's
   review, or wait and ship the whole thing in 3 days?

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (optional for skill redesign) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | substituted via Claude subagent below |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 4 architecture + 3 code-quality issues raised, all resolved; 1 critical test gap closed |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | no UI in this plan |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | n/a |

**OUTSIDE VOICE (Claude subagent, fresh context):** 9 findings; 6 resolved via user decisions (O1, O2, O4, O7) + 3 inline plan updates (`later` semantics, phase decoupling, migration rollback).

**CROSS-MODEL:** primary review found 7 issues; outside voice found 9. 2 overlap (defensive parser exit on empty input; `later` status ambiguity). Outside voice added the lineage-matching-during-daily-input gap (O1) — the biggest miss of the original plan.

**UNRESOLVED:** 0.

**VERDICT:** ENG CLEARED — ready to implement. Phase 1 ships today; Phase 2-5 within 3 days.

