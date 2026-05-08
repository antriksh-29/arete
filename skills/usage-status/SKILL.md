---
name: usage-status
version: 0
script: infra/usage-status.py
mutating: false
cron: null
eval: skills/usage-status/eval.yaml
last_audited: 2026-05-08
description: |
  Today's (or a specific day's) LLM spend / token usage report. Backs
  AGENTS.md Rule 14 ("If user asks 'usage', 'spend', 'how much have I
  spent today', 'tokens today', or similar, run the usage-status skill").
  WHEN: "usage", "spend", "how much have I spent today", "tokens today",
  "what did I burn today", "API cost today".
  NOT WHEN: User asks about brain storage usage (→ gbrain stats) or VPS
  disk usage (→ shell). NOT WHEN they ask about subscription state.
trigger: |
  - "usage" / "usage today" / "what did I burn today"
  - "spend today" / "spend yesterday"
  - "how much have I spent today"
  - "tokens today" / "API cost today"
  - "what's my spend"
writes_to: []
brain_dirs_read: []
---

# /usage-status

**Status: PLACEHOLDER (version 0).** The SKILL.md, eval, and resolver
entry are wired so the audit gate is clean. The backing script
`infra/usage-status.py` returns a clear "not yet wired" envelope —
real cost-log read lands in Wave 4B once the OpenClaw cost-log path on
the VPS is located.

## How to invoke

```bash
~/bin/usage-status.py             # today
~/bin/usage-status.py yesterday   # yesterday
~/bin/usage-status.py 2026-05-04  # specific date
```

## Resolver patterns

| User intent | Command |
|---|---|
| "usage" / "spend today" / "tokens today" | `usage-status.py` |
| "spend yesterday" / "yesterday's usage" | `usage-status.py yesterday` |
| "spend on May 4" | `usage-status.py 2026-05-04` |

## Telegram reply

Relay the script's `user_message` field verbatim. While in placeholder
state the message explains the limitation honestly — don't make up
numbers.

## Wave 4B — wiring the real read

Probable cost-log locations on the VPS to check:
- `~/.openclaw/logs/usage.jsonl`
- `~/.openclaw/cost.log`
- `journalctl --user -u openclaw-gateway | grep -i cost`

Once located, replace the placeholder body in `infra/usage-status.py`
with a real read + aggregation, bump SKILL.md `version: 1`, and
populate output evals against a known-good fixture.

## Architectural rules

- **Never fabricate numbers.** If the cost log isn't readable, say so.
- **Don't query the LLM provider's API directly** — billing endpoints
  rate-limit aggressively and aren't designed for per-call reads.
  OpenClaw's local cost log is the right source.

## Smoke

The script supports `--smoke` (returns a static envelope, no log read).
