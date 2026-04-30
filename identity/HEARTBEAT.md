# Heartbeat checklist

Items the agent checks each time the heartbeat fires. Read on every fire.

If anything below needs the user's attention, message him on Telegram.
Otherwise reply `HEARTBEAT_OK` and stay silent.

---

## Current state: STAGE 1 — DISABLED

**This file is intentionally minimal.** Heartbeat config is `every: "0m"`
(disabled) per PRD §13.1 Stage 1. No autonomous fires happen. The file exists
so OpenClaw can find it later when we advance to Stage 2-4.

The PRD/eng plan staging:

| Stage | When | `every` | `target` | What |
|-------|------|---------|----------|------|
| 1 — Disabled | Now (~week 1) | `0m` | n/a | Reactive only. Build trust in AGENTS.md against real ingested content. |
| 2 — Drafting | ~week 2 | `0m` | n/a | Accumulate items below from real "wish it had pinged me" moments. |
| 3 — Observation-only | ~week 3 | `1h` | `none` | Heartbeat fires, writes findings to `reports/heartbeat-YYYY-MM-DD.md`, doesn't message Antriksh. |
| 4 — Active delivery | ~week 4 | `1h` | `last` | Full experience. Surfaces actionable items to Telegram. |

**During Stage 1:** every time Antriksh notices "I wish the bot had pinged
me about X," he should add it as a checklist item below. No guesswork.
Items added without a real moment of need are likely to be noise.

---

## Quiet hours

Active hours: 08:00–22:00 IST. Outside this window: skip.

Per AGENTS.md and USER.md, no proactive surfaces in quiet hours unless
P0/safety-relevant.

---

## Checklist

(Empty on purpose. Populate during Stage 2 from real misses.)

Future format, for reference:

```
## Calendar awareness
- Any meeting starting in next 30 minutes that lacks a prep dossier?
  If yes, draft one and offer it.
- Any meeting today with someone Antriksh hasn't talked to in >7 days?
  Surface a quick refresher.

## Inbox awareness
- Any unread email in `Bot/Ingest` label tagged urgent or from
  [list of high-priority senders]?

## Task hygiene
- Any P0 task that hasn't moved in >24 hours?

## Brain hygiene
- Any orphan person/company page created in last 24h with zero inbound
  links — likely a stub that needs enrichment.
```

Don't pre-fill these. The Stage 2 exercise is about discovering which items
genuinely matter for *this user* in *his actual workflow*, not picking from
a generic list.

---

## Stage advancement criteria (from eng plan §A10)

Stage 1 → Stage 2: ≥7 days of reactive operation, AGENTS.md hardened,
≥1 real-world prompt-injection refusal in ingested content.

Stage 2 → Stage 3: HEARTBEAT.md has 4-8 specific items, all sourced from
real "wish it had pinged me" moments, AGENTS.md edited at least once based
on real bot behavior.

Stage 3 → Stage 4: ≥7 days at Stage 3, ≥30 surfaces graded, 7-day rolling
useful/(useful+noisy+wrong) ratio ≥ 0.70, zero `wrong` flagged as
safety-relevant in last 7 days.

Each gate is the user's decision, not the bot's. The bot may surface
"ready for advancement" via the Sunday hygiene cron, but advancement is
a deliberate act.
