---
name: skill-drift
version: 1
script: infra/skill-drift-report.sh
mutating: false
cron: "0 9 * * 0"   # Sunday 09:00 IST — wired into morning briefing
eval: skills/skill-drift/eval.yaml
last_audited: 2026-05-08
description: |
  Weekly skill-catalog health check. Surfaces three kinds of silent
  decay: usage drift (which skills are firing more / less than usual),
  quality drift (which scripts return errors), and dormant skills
  (fired 0× in 30 days — candidates for retirement).
  WHEN: User asks "skill drift" / "which skills are dormant" / "skill
  health" / "skill catalog status" / "what skills are broken". Also
  fires automatically Sunday 09:00 IST as part of the weekly briefing.
  NOT WHEN: User asks about a specific skill's status (→ skillify
  audit) or about LLM cost (→ usage-status). NOT for live debugging
  of a single skill (→ inspect journalctl directly).
trigger: |
  - "skill drift" / "skill drift report"
  - "which skills are dormant"
  - "skill health" / "skill catalog status"
  - "what skills are broken"
  - "skill activity last week" / "skill activity last 30 days"
  - "are any skills failing"
writes_to: []
brain_dirs_read: []
---

# /skill-drift

Single source of truth: `infra/skill-drift-report.sh` (deployed to
`~/bin/`). Reads journalctl + per-skill state file mtimes + the
gbrain smoke-test log; produces a structured report with one row per
skill.

## How to invoke

```bash
~/bin/skill-drift-report.sh                # JSON, last 7 days
~/bin/skill-drift-report.sh --days 30      # different window
~/bin/skill-drift-report.sh --markdown     # human-readable
~/bin/skill-drift-report.sh --smoke
```

## What each row means

| Field | Meaning |
|---|---|
| `journal_hits` | grep count of the skill's script basename in journalctl over the window |
| `state_freshness` | `fresh` if any state file matching the skill's name was modified; `stale` if not; `no-state-root` if `~/.brain-bootstrap-raw/state/` doesn't exist |
| `smoke` | `pass` / `fail` / `absent` from the gbrain smoke-test log; `no-log` if the log isn't present |
| `status` | `active` / `dormant` (no journal + stale state) / `errored` (smoke fail) / `constraint` (constraint skills don't have user-fired counts) |

## Three drift signals

1. **Usage drift** — a skill's `journal_hits` sharply higher or lower
   than its baseline. Today the report shows raw counts; baseline
   diffing is a Wave 5 follow-up.
2. **Quality drift** — `smoke=fail` means the skill's `--smoke` is
   returning ok=false or non-zero exit. Investigate the script.
3. **Dormancy** — `status=dormant` for ≥ 30 days. Either the trigger
   phrasing has shifted (rewrite the SKILL.md trigger block), or the
   skill is genuinely unused (retire it).

## Resolver patterns

| User intent | Command |
|---|---|
| "skill drift" / "skill health" | `bash ~/bin/skill-drift-report.sh --markdown` |
| "which skills are dormant" | `bash ~/bin/skill-drift-report.sh` and surface only `status=dormant` rows |
| "skills failing this week" | `bash ~/bin/skill-drift-report.sh` and surface only `status=errored` rows |

## Telegram reply

For ad-hoc queries, relay the markdown table verbatim. For the Sunday
briefing entry, surface only the SUMMARY line + dormant/errored counts;
the full table is too long for a briefing slot.

## Architectural rules

- **Don't auto-retire dormant skills.** Surface, don't act. Retirement
  is a deliberate decision the user makes after reading the report.
- **Per-skill baseline diffing is a Wave 5 follow-up.** Today's report
  is raw counts; "more / less than usual" judgment is a future feature.
- **Constraint skills (reply-discipline) are exempt from dormancy
  checks.** They run on every reply, so journal-hit counts don't
  reflect user invocation.

## Smoke

`skill-drift-report.sh --smoke` returns a static envelope (no
journalctl, no state-file scan).
