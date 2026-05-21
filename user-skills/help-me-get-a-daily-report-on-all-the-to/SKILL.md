---
name: help-me-get-a-daily-report-on-all-the-to
version: 0.1.1
description: "help me get a daily report on all the top yc company launches/announcements made by yc itself in the last 24 hours deliver this daily to me at 6:30pm 1. top here can mean announced by yc 2. format should be bullet points with one line description along with the yc post link 3. just launches - new companies coming out of the latest batch - Poll YC's launch page daily - Filter to new companies from the latest batch - Deliver a bullet list like above at 6:30 PM IST\n\n  WHEN: Runs automatically on schedule `30 18 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15)."
trigger: []
created_by: alfred
source: telegram
created_at: "2026-05-21T12:42:59.488Z"
last_authoring_turn: "2026-05-21T12:52:39.853Z"
mutating: false
cron: "30 18 * * *"
eval: user-skills/help-me-get-a-daily-report-on-all-the-to/eval.yaml
---
# /help-me-get-a-daily-report-on-all-the-to

Author: Alfred (Telegram skill-author, 2026-05-21)

## Capability

help me get a daily report on all the top yc company launches/announcements made by yc itself in the last 24 hours deliver this daily to me at 6:30pm 1. top here can mean announced by yc 2. format should be bullet points with one line description along with the yc post link 3. just launches - new companies coming out of the latest batch - Poll YC's launch page daily - Filter to new companies from the latest batch - Deliver a bullet list like above at 6:30 PM IST

## When to fire

Automatically on schedule: `30 18 * * *` (Asia/Kolkata).
Fires once per day at 18:30 IST.

## Output

chat message here on telegram

## Notes

- Authored from Telegram on 2026-05-21.
- Edit by saying `/refine help-me-get-a-daily-report-on-all-the-to` in Telegram, or hand-edit this file in Claude Code.
- See `brain/skills/user-skills/help-me-get-a-daily-report-on-all-the-to.md` for the original elicit Q&A.
