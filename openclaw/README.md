# openclaw/

OpenClaw config and custom skill source. Deployed to `~/.openclaw/workspace/` on the VPS.

| File | Phase | Purpose |
|------|-------|---------|
| `config.json` | 3 | Main OpenClaw config: channels, models, tools, sessions, heartbeat (Stage 1 = `every: 0m`). |
| `_smoke/injection-fixture.yaml` | 2 | 30 prompt-injection patterns. Smoke test runs this on every restart; 100% refusal required. |
| `_eval/retrieval-eval.yaml` | 4 | 10 hand-labeled queries, grow to 30. nDCG@3 baseline. |
| `crons/*.yaml` | 6 | One file per scheduled job: morning_briefing, eod_capture, weekly_review, etc. |
| `skills/twitter-ingest/SKILL.md` | 5 | Custom skill: forward tweet → ingest. |
| `skills/usage-status/SKILL.md` | 5 | Custom skill: today's spend report. |
| `skills/twitter-daily-summary/SKILL.md` | 5 | Custom skill: nightly aggregate. |

Phase 0 status: directory exists, files authored in later phases.
