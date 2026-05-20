---
title: steve-jobs-facts
slug: user-skills/steve-jobs-facts
created: 2026-05-20
authored_via: Telegram (hand-completed by Layer B cleanup after rogue-write bypass)
mode: create
---

# steve-jobs-facts — authoring log

## Original intent (image-v18 conversation transcript, 2026-05-20)

User: "Do we have a Steve Jobs facts skill?"

Bot offered to create one. User confirmed with "Understood — let's do
this then" and provided the framing: "whenever I mentioned something
along the lines of 'steve jobs facts' — you should share an
interesting fact about him with me".

User then asked to expand from 10 facts to 20 facts, and to make sure
the skill rotates so repeats are rare.

## Elicit equivalents (reconstructed)

| Q | A |
|---|---|
| Q1 capability | Share an interesting fact about Steve Jobs |
| Q2 invocation_mode | manual |
| Q3 triggers | "steve jobs facts", "tell me something about steve jobs", "steve jobs fact", "something about steve jobs" |
| Q5 output | One fact in this chat, 1-3 sentences, drawn from a curated pool of 20 facts |

(Q4 doesn't apply — manual skills skip the schedule question.)

## Why this was hand-authored rather than going through elicit

The Telegram skill-author flow was bypassed by the model in earlier
attempts. The bot wrote SKILL.md files to `~/.openclaw/workspace/skills/`
via raw `exec` and replied "Done. Created skills/steve-jobs-facts/" —
fabricated success, no git commit, no deploy callback. PR #109
hardened the rules but the next bypass attempt landed in
`workspace/skills/steve-jobs-facts/` as well.

The hand-author here moves the skill into the correct location
(`user-skills/`) and commits it through the normal git flow so it
actually exists in the repo and survives deploys.

A separate follow-up (Layer A) installs an audit daemon that reverts
any rogue write to `skills/` paths outside the elicit pipeline,
preventing this class of bug structurally.

## Gate verdicts

- safety-scan: skipped (no IOCs — pure declarative SKILL.md + plain-text fact pool)
- routing-eval: skipped (4 distinct trigger phrases that don't collide with existing skills; verified manually against `grep -r "steve jobs" skills/`)
- commit: see git log for the merge commit
