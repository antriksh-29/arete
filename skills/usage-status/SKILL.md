---
name: usage-status
version: 1
script: infra/usage-status.py
mutating: false
cron: null
eval: skills/usage-status/eval.yaml
last_audited: 2026-05-08
description: |
  Today's (or a specific day's) LLM spend + token usage report.
  Reads OpenClaw's canonical cost data via `openclaw gateway
  usage-cost --days N --json`; falls back to the cache file at
  ~/.openclaw/state/agents/default/sessions/.usage-cost-cache.json
  if the CLI is unreachable. Returns a structured row with input /
  output / cacheRead / cacheWrite tokens and per-bucket cost.
  WHEN: "usage", "spend", "how much have I spent today", "tokens
  today", "what did I burn today", "API cost today", "cost on May 4".
  NOT WHEN: User asks about brain storage usage (→ gbrain stats) or
  VPS disk usage (→ shell). NOT WHEN they ask about subscription
  state — billing isn't here.
trigger: |
  - "usage" / "usage today" / "what did I burn today"
  - "spend today" / "spend yesterday"
  - "how much have I spent today"
  - "tokens today" / "API cost today"
  - "what's my spend"
  - "cost on <date>"
writes_to: []
brain_dirs_read: []
---

# /usage-status

Backed by `infra/usage-status.py`. Three sources tried in order:

1. **OpenClaw CLI** — `openclaw gateway usage-cost --days N --json`.
   Most authoritative. Uses OpenClaw's own cost computation which
   handles cache-read / cache-write pricing correctly per model.
2. **Cache file fallback** — directly aggregates today's entries from
   `~/.openclaw/state/agents/default/sessions/.usage-cost-cache.json`
   using IST day boundaries. Used if CLI errors (lockfile contention,
   binary missing).
3. **Honest failure** — if both unreachable, returns `ok: false` with
   a message pointing the user at `systemctl --user status
   openclaw-gateway` and `openclaw doctor`. Per Rule 14: spend
   visibility, not spend hallucination.

## How to invoke

```bash
~/bin/usage-status.py                # today (IST)
~/bin/usage-status.py yesterday      # yesterday IST
~/bin/usage-status.py 2026-05-04     # specific date
~/bin/usage-status.py --days 7       # rolling 7-day window
~/bin/usage-status.py --smoke
```

## Result envelope

```json
{
  "ok": true,
  "skill": "usage-status",
  "source": "openclaw cli" | "cache file",
  "scope": "2026-05-08",
  "days_window": 1,
  "row": {
    "date": "2026-05-08",
    "input": 1500, "output": 800,
    "cacheRead": 200, "cacheWrite": 50,
    "totalTokens": 2550,
    "inputCost": 0.0045, "outputCost": 0.012,
    "cacheReadCost": 0.0001, "cacheWriteCost": 0.0002,
    "totalCost": 0.0168
  },
  "user_message": "💰 Spend on 2026-05-08: $0.0168 · 2,550 tokens ..."
}
```

On honest failure: `ok: false`, `source: null`, `user_message`
explaining the unreachable surfaces.

## Resolver patterns

| User intent | Command |
|---|---|
| "usage" / "spend today" / "tokens today" | `usage-status.py` |
| "spend yesterday" | `usage-status.py yesterday` |
| "spend on May 4" | `usage-status.py 2026-05-04` |
| "spend last 7 days" | `usage-status.py --days 7` |

## Telegram reply

Relay the `user_message` field verbatim. The format is already crisp
for Telegram (one line, emoji prefix, in/out/cache breakdown). Don't
add commentary about model choice, rate limits, or provider.

## Architectural rules

- **Never fabricate numbers.** If both data paths fail, surface the
  failure honestly — don't estimate.
- **Don't hit the LLM provider's billing API directly.** Billing
  endpoints rate-limit and aren't designed for per-call reads.
  OpenClaw's local data is the canonical source.
- **IST day boundaries.** "Today" means today in Asia/Kolkata, not
  UTC. Both code paths respect this.
- **Soft alert at $5/day** lives in AGENTS.md Rule 14 — it reads the
  same source as this skill, but is fired by the daily-brief
  orchestrator, not by user invocation.

## Smoke

`usage-status.py --smoke` returns a static envelope. No CLI call,
no cache read.
