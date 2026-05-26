---
name: paul-graham-insights
version: 0.1.1
description: |
  Share a powerful insight from Paul Graham's essays.

  WHEN: Runs automatically on schedule `30 17 * * *` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks ("paul graham insight", "pg insight", "share a pg essay insight").
  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15).
trigger:
  - "paul graham insight"
  - "pg insight"
  - "share a pg essay insight"
created_by: alfred
source: telegram
created_at: "2026-05-20T13:55:00.000Z"
last_authoring_turn: "2026-05-26T11:17:26.028Z"
mutating: false
cron: "30 17 * * *"
eval: user-skills/paul-graham-insights/eval.yaml
---

# /paul-graham-insights

Author: Alfred (Telegram skill-author, 2026-05-20)

## Capability

Share one powerful insight from Paul Graham's essays. Rotates through
a curated pool of 15 insights, framed as a short daily insight for the
user.

## When to fire

Automatically on schedule: `30 17 * * *` (Asia/Kolkata).
Fires once per day at 17:30 IST.

Manual triggers (optional):

- "paul graham insight"
- "pg insight"
- "share a pg essay insight"

## Output

One PG insight in this chat — 2-4 sentences, drawn from the pool
below. Pick uniformly at random; if a recent state file
(`~/.brain-bootstrap-raw/state/paul-graham-insights.json`) is present,
prefer an insight NOT in the last 5 emitted; otherwise pick freely.

Format: just the insight, with the essay title/year as a one-line
footer (e.g. `— Paul Graham, "Do Things That Don't Scale" (2013)`).
No preamble. No commentary.

## Insight pool (15)

1. The most successful startups in YC's experience took longer than expected to find product-market fit. The founders who pushed through "looks like nothing's working" were the ones who reached escape velocity. — Paul Graham, "Do Things That Don't Scale" (2013)
2. Default Alive vs Default Dead: at the current growth rate and burn, will you reach profitability before money runs out? Founders systematically overestimate their position; calculate honestly. — Paul Graham, "Default Alive or Default Dead?" (2015)
3. Make something people want. Not: make something people might want. Not: build a platform. The default state of any product is "no one cares" — your job is to overcome that with a thing humans actively want. — Paul Graham, "How to Get Startup Ideas" (2012)
4. Live in the future, then build what's missing. The best startup ideas come from founders who are themselves users frustrated by what doesn't exist yet, not from market-gap analyses. — Paul Graham, "How to Get Startup Ideas" (2012)
5. Schlep blindness: founders avoid ideas that involve unglamorous, painful work — exactly the ideas with the least competition. Stripe, Airbnb, and DoorDash all required schleps others wouldn't touch. — Paul Graham, "Schlep Blindness" (2012)
6. Maker's Schedule vs Manager's Schedule: a single meeting in the middle of a maker's day can blow the whole afternoon by destroying flow state. Protect long uninterrupted blocks for builders. — Paul Graham, "Maker's Schedule, Manager's Schedule" (2009)
7. The acid test for a founder: can you write a one-sentence description of what your company does that the listener immediately gets? If not, you don't have product-market clarity yet. — Paul Graham, "How to Convince Investors" (2013)
8. Founder mode: the largest companies are often run by their founders in ways consultants would call wrong — direct contact with line workers, skipping reporting layers, idiosyncratic decision-making. It scales because of, not despite, this. — Paul Graham, "Founder Mode" (2024)
9. The best way to predict a founder's success is whether they're "relentlessly resourceful". It beats experience, IQ, and credentials. Synonyms: gritty, bound to find a way. — Paul Graham, "Relentlessly Resourceful" (2009)
10. Y Combinator's #1 piece of advice is "talk to users". The reason it works isn't because users know what they want — it's because their problems become inarguable when you watch them struggle. — Paul Graham, "Do Things That Don't Scale" (2013)
11. Cities tell you what to do. Berkeley says "live well"; New York says "make more money"; Silicon Valley says "be more powerful". Choose the city whose message matches your goal. — Paul Graham, "Cities and Ambition" (2008)
12. The Bus Ticket Theory of Genius: outsized achievement requires a near-pathological obsession with a narrow domain. Not "passion" in the LinkedIn sense — actual inability to think about anything else. — Paul Graham, "The Bus Ticket Theory of Genius" (2019)
13. To get rich, start a company that will be valuable in 30 years. Companies that play a long game compound; companies that optimize for the next quarter eventually break. — Paul Graham, "How to Make Wealth" (2004)
14. Wealth is the moral equivalent of money created — fixed-pie thinking is the enemy of building. The world is not zero-sum; making something valuable makes everyone richer. — Paul Graham, "Mind the Gap" (2004)
15. The most important thing a startup can do is not run out of money. The second most important is not lose its momentum. Founder energy is the actual currency that compounds — protect it. — Paul Graham, "Startup = Growth" (2012)

## Notes

- Authored from Telegram on 2026-05-20.
- Edit by saying `/refine paul-graham-insights` in Telegram, or hand-edit this file in Claude Code.
- See `brain/skills/user-skills/paul-graham-insights.md` for the original elicit Q&A.
