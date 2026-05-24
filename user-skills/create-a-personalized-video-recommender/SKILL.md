---
name: create-a-personalized-video-recommender
version: 0.1.1
description: "Create a personalized video recommender from YouTube for the user, selecting from two specified channels.\n\n  WHEN: Runs automatically on schedule `50 16 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15)."
trigger: []
created_by: alfred
source: telegram
created_at: "2026-05-22T05:25:56.910Z"
last_authoring_turn: "2026-05-22T06:02:34.587Z"
mutating: false
cron: "50 16 * * *"
eval: user-skills/create-a-personalized-video-recommender/eval.yaml
---
# /create-a-personalized-video-recommender
Author: Alfred (Telegram skill-author, 2026-05-22)

## Capability
Generates a personalized video recommendation from YouTube daily, selecting from two user-specified channels.

## Channels
- https://youtube.com/@ycombinator?si=oUEDL9n5uvr5xMxl
- https://youtube.com/@ycrootaccess?si=fGyn0R3KOUK7wm8i

## How it works
- At 11:45 AM IST, asks the user what kind of video they want to watch today
- Waits for the user's reply before selecting any video
- Fetches recent videos from the two specified YouTube channels only
- Selects the most relevant video based on the user's topic
- Formats the recommendation with the video title, description, and YouTube link
- Delivers the recommendation as a Telegram chat message right after the user's reply

## Recommendation Rules
- Only use the two channels listed above.
- Do not recommend a video until the user has replied with a topic.
- Start with a question like: "What kind of video do you want to watch today?"
- Return exactly:
  - video title
  - video description
  - video link

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
