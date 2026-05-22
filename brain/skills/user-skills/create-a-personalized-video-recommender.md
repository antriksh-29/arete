---
type: user-skill
created: 2026-05-22
updated: 2026-05-22
tags: [user-skill, alfred-authored]
---
# user-skill: create-a-personalized-video-recommender
_Authored fresh via Telegram._
**Initial commit:** `5a0120a`
## Elicit Q&A
- **Q1 (capability):** Create a personalized video recommender from YouTube for the user, selecting from two specified channels.
- **Q2 (triggers):** 
- **Q3 (output):** Telegram chat message with video title, description, and link.
- **Q4 (tests):** no
## Connections
- Skill file: [[../../../user-skills/create-a-personalized-video-recommender/SKILL.md]]
- Eval file: [[../../../user-skills/create-a-personalized-video-recommender/eval.yaml]]
## Refinement turn (2026-05-22, commit `c088698`)

- **Change requested:** add these two channels for the Skill create-a-personalized-video-recommender

1. https://youtube.com/@ycombinator?si=oUEDL9n5uvr5xMxl
2. https://youtube.com/@ycrootaccess?si=fGyn0R3KOUK7wm8i

also, please change the timing of this skill to 11:45am daily
- **New behavior:** Added two new YouTube channels to the video recommender skill; Changed the skill's firing time to 11:45 AM daily
