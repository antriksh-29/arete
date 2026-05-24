---
name: yc-launches-daily-briefings
version: 0.2.0
description: "Daily YC launches briefing — bullet list of new companies from the latest YC batch announced in the last 24 hours, with one-line description and YC post link.\n\n  WHEN: Runs automatically on schedule `15 16 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15)."
trigger: []
created_by: alfred
source: telegram
created_at: "2026-05-21T12:42:59.488Z"
last_authoring_turn: "2026-05-24T18:00:00.000Z"
renamed_from: "help-me-get-a-daily-report-on-all-the-to"
mutating: false
cron: "15 16 * * *"
cron: "50 16 * * *"
---
# /yc-launches-daily-briefings

Author: Alfred (Telegram skill-author, 2026-05-21 — renamed 2026-05-24)

## Capability

Daily briefing of new YC company launches from the latest batch over the last 24 hours. Formatted as a bullet list, one line per company, with the YC post link.

## How it works

Implemented as a deterministic Python fetcher (`infra/yc-launches-daily.py`):
- Hits `https://www.ycombinator.com/launches` with `Accept: application/json` — returns the launches feed as JSON.
- Auto-detects the latest batch from the most-common `company.batch` across recent hits (currently `Spring 2026`).
- Filters to launches in the last 24h within that batch.
- Formats as `• <b>CompanyName</b> — tagline\n  <YC link>` and sends via `send-telegram.sh --html`.
- Zero LLM in the loop — runs in <2s, no cold-start failures.

The OpenClaw cron job's payload exec's this script (with `lightContext: true` and `timeoutSeconds: 180`).

## When to fire

Automatically on schedule: `15 16 * * *` (Asia/Kolkata).
Fires once per day at 16:15 IST.

## Output format

```
YC Daily Launches — May 24, 2026
Latest batch: Spring 2026

• Mount — As AI Agents move into production, we help businesses insure and secure them.
  https://www.ycombinator.com/launches/QUI-mount-the-ai-agent-insurance-carrier

• AbInitio Bio — Foundation models for Biomanufacturing
  https://www.ycombinator.com/launches/QS3-abinitio-bio-frontier-foundation-models-for-biomanufacturing
```

## Notes

- Renamed from `help-me-get-a-daily-report-on-all-the-to` on 2026-05-24.
- Edit by saying `/refine yc-launches-daily-briefings` in Telegram, or hand-edit this file in Claude Code.
- Underlying script: `infra/yc-launches-daily.py`. Run `--dry-run` to preview without sending.
