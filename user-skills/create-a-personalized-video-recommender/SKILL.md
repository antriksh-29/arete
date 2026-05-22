---
name: create-a-personalized-video-recommender
version: 0.1.1
description: "Create a personalized video recommender from YouTube for the user, selecting from four specified channels.\n\n  WHEN: Runs automatically on schedule `45 11 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15)."
trigger: []
created_by: alfred
source: telegram
created_at: "2026-05-22T05:25:56.910Z"
last_authoring_turn: "2026-05-22T06:02:34.587Z"
mutating: false
cron: "45 11 * * *"
eval: user-skills/create-a-personalized-video-recommender/eval.yaml
---
# /create-a-personalized-video-recommender
Author: Alfred (Telegram skill-author, 2026-05-22)

## Capability
Generates a personalized video recommendation from YouTube daily, selecting from four user-specified channels.

## How it works
- Fetches recent videos from the four specified YouTube channels
- Selects one video based on a predefined algorithm
- Formats the recommendation with the video title, description, and YouTube link
- Delivers the recommendation as a Telegram chat message

## When to fire
Fires once per day at 11:45 AM IST.
```
45 11 * * *
```

## Output format
```
🎥 Today's recommended video:

**Title:** The Future of AI
**Description:** Explore the latest advancements in artificial intelligence and machine learning.
**Link:** [Watch on YouTube](https://www.youtube.com/watch?v=dQw4w9WgXcQ)
```

## Notes
- Authored from Telegram on 2026-05-22.
- Edit by saying `/refine create-a-personalized-video-recommender` in Telegram, or hand-edit this file in Claude Code.
- See `brain/skills/user-skills/create-a-personalized-video-recommender.md` for the original elicit Q&A.

---