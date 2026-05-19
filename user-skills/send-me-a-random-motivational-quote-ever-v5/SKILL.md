---
name: send-me-a-random-motivational-quote-ever-v5
version: 0.1.0
description: "send me a random motivational quote every morning\n\n  WHEN: Runs automatically on schedule `00 08 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15)."
trigger: []
created_by: alfred
source: telegram
created_at: "2026-05-19T13:22:55.636Z"
last_authoring_turn: "2026-05-19T13:22:55.636Z"
mutating: false
cron: "00 08 * * *"
eval: user-skills/send-me-a-random-motivational-quote-ever-v5/eval.yaml
---
# /send-me-a-random-motivational-quote-ever-v5

Author: Alfred (Telegram skill-author, 2026-05-19)

## Capability

send me a random motivational quote every morning

## When to fire

Automatically on schedule: `00 08 * * *` (Asia/Kolkata).
Fires once per day at 08:00 IST.

## Output

reply in this chat

## Notes

- Authored from Telegram on 2026-05-19.
- Edit by saying `/refine send-me-a-random-motivational-quote-ever-v5` in Telegram, or hand-edit this file in Claude Code.
- See `brain/skills/user-skills/send-me-a-random-motivational-quote-ever-v5.md` for the original elicit Q&A.
