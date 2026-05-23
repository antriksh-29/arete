---
name: change-the-hackernews-daily-briefing-sch
version: 0.1.0
description: "Change the HackerNews daily briefing schedule to deliver at 8pm daily.\n\n  WHEN: Runs automatically on schedule `00 20 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15)."
trigger: []
created_by: alfred
source: telegram
created_at: "2026-05-23T14:24:43.625Z"
last_authoring_turn: "2026-05-23T14:24:43.625Z"
mutating: false
cron: "00 20 * * *"
eval: user-skills/change-the-hackernews-daily-briefing-sch/eval.yaml
---
# /change-the-hackernews-daily-briefing-sch

Author: Alfred (Telegram skill-author, 2026-05-23)

## Capability

Delivers a daily briefing of top HackerNews articles at 8:00 PM IST.

## How it works

- Polls HackerNews for the top articles of the day
- Formats each article as a concise summary with the title and link
- Delivers the briefing as a Telegram message at the scheduled time

## When to fire

Fires once per day at 20:00 IST.

```
00 20 * * *
```

## Output format

```
Top Stories on HackerNews:

1. Title of Article 1
   https://news.ycombinator.com/item?id=12345678

2. Title of Article 2
   https://news.ycombinator.com/item?id=87654321

...
```

## Notes

- Authored from Telegram on 2026-05-23.
- Edit by saying `/refine change-the-hackernews-daily-briefing-sch` in Telegram, or hand-edit this file in Claude Code.
- See `brain/skills/user-skills/change-the-hackernews-daily-briefing-sch.md` for the original elicit Q&A.

---