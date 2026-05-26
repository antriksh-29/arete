---
title: paul-graham-insights
slug: user-skills/paul-graham-insights
created: 2026-05-20
authored_via: Telegram (hand-completed by Layer B cleanup after rogue-write bypass)
mode: create
---

# paul-graham-insights — authoring log

## Original intent (image-v16 + image-v19 transcript, 2026-05-20)

User: "share a powerful insight from Paul Graham's essays every day at
the following times: 6:30 PM — starting today"

Bot proposed: "Cron job: paul-graham-insight, Schedule: every day at
6:30 PM IST, Skill created: skills/paul-graham-insights/SKILL.md with
15 curated insights from PG's essays. It'll rotate through the list
so repeats are rare. The skill also has manual triggers ("paul graham
insight", "pg insight") if you ever want one on demand."

User: "let the cron be 6:30pm today" (confirmed schedule)

Bot: "Got it. The cron will fire at 6:30 PM IST today — in about 16
minutes from now."

## Elicit equivalents (reconstructed)

| Q | A |
|---|---|
| Q1 capability | Share a powerful insight from Paul Graham's essays |
| Q2 invocation_mode | periodic |
| Q3 frequency | daily |
| Q4 time_of_day | 18:30 (IST) |
| Q5 output | A 2-4 sentence insight in this chat with the essay title/year as a one-line footer |

Manual trigger phrases (in addition to the cron): "paul graham insight",
"pg insight", "share a pg essay insight".

## Why this was hand-authored rather than going through elicit

Same root cause as steve-jobs-facts — the Telegram skill-author flow
was bypassed by the model. Bot wrote SKILL.md to `~/.openclaw/workspace/skills/paul-graham-insights/`
via raw `exec` and replied "Done. Created…" without going through the
elicit + safety + routing + commit + deploy pipeline. The user saw
fake success in Telegram but no skill landed in git or fired any
deploy callback.

This hand-author moves the skill into the correct location
(`user-skills/`) and commits it through the normal git flow so it
actually exists in the repo and survives deploys.

Layer A (separate task) installs a structural enforcement daemon so
the bypass can't recur.

## Gate verdicts

- safety-scan: skipped (no IOCs — pure declarative SKILL.md + plain-text insight pool)
- routing-eval: skipped (3 distinct trigger phrases that don't collide; verified manually against existing skills)
- commit: see git log for the merge commit

## Refinement turn (2026-05-21, commit `113e90b`)

- **Change requested:** can you please make paul-graham-insights also include the source URL for each insight going forward
- **New behavior:** Include the source URL for each Paul Graham insight in the output

## Refinement turn (2026-05-26, commit `f4d44f2`)

- **Change requested:** change the timing of the paul graham insights to 5pm daily
- **New behavior:** moved your daily Paul Graham insights to 5pm
