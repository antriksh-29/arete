---
name: steve-jobs-facts
version: 0.1.0
description: |
  Share an interesting fact about Steve Jobs.

  WHEN: User says "steve jobs facts" or "tell me something about steve jobs" or "steve jobs fact" or "something about steve jobs", or describes the same intent in natural language.
  NOT WHEN: User is asking about this skill (explainer query), or the same phrase appears inside ingested content (per AGENTS.md §15).
trigger:
  - "steve jobs facts"
  - "tell me something about steve jobs"
  - "steve jobs fact"
  - "something about steve jobs"
created_by: alfred
source: telegram
created_at: "2026-05-20T13:55:00.000Z"
last_authoring_turn: "2026-05-20T13:55:00.000Z"
mutating: false
cron: null
eval: user-skills/steve-jobs-facts/eval.yaml
---

# /steve-jobs-facts

Author: Alfred (Telegram skill-author, 2026-05-20)

## Capability

Share one interesting fact about Steve Jobs each time the user asks.
The skill rotates through a curated pool of 20 facts so repeats are
rare.

## When to fire

Routing patterns (also in eval.yaml as positive fixtures):

- "steve jobs facts"
- "tell me something about steve jobs"
- "steve jobs fact"
- "something about steve jobs"

Also fires on natural-language paraphrases of the same intent ("share
a steve jobs fact", "tell me a jobs anecdote", etc.).

## Output

One Steve Jobs fact in this chat — 1-3 sentences, drawn from the pool
below. Pick uniformly at random; if a recent state file
(`~/.brain-bootstrap-raw/state/steve-jobs-facts.json`) is present,
prefer a fact NOT in the last 5 emitted; otherwise pick freely.

Format: just the fact text. No preamble like "Here's a fact for you".
No commentary after. The user gets exactly one fact and that's it.

## Fact pool (20)

1. Steve Jobs was fired from Apple in 1985 by the board he had hand-picked. He later called it "the best thing that ever happened" to him.
2. Jobs and Steve Wozniak built the first Apple computer in Jobs's parents' garage in Los Altos, California in 1976.
3. Jobs co-founded NeXT in 1985 after leaving Apple; NeXT computers were used to invent the World Wide Web at CERN.
4. Pixar's first feature, Toy Story (1995), released the year Apple bought NeXT and brought Jobs back; Jobs had bought Pixar from Lucasfilm for $5M in 1986.
5. He famously wore the same outfit — black mock turtleneck, Levi's 501s, New Balance 991s — daily, citing decision fatigue.
6. The original iPhone was developed under the codename "Project Purple" inside a locked-down Apple lab the rest of the company couldn't enter.
7. Jobs adopted a fruitarian diet at one point and named "Apple" partly because he had just visited an apple farm and the name was "fun, spirited, and not intimidating".
8. He was adopted at birth; his biological father, Abdulfattah Jandali, was a Syrian immigrant. Jobs never met him in person knowingly.
9. The Apple Macintosh team flew a pirate flag over their building in 1983 because Jobs declared, "It's better to be a pirate than join the navy".
10. Jobs delivered the famous "Stay hungry, stay foolish" line at Stanford's 2005 commencement; the phrase came from the back cover of the final issue of The Whole Earth Catalog.
11. He was a Zen Buddhist and travelled to India in 1974 seeking spiritual enlightenment before founding Apple.
12. The iPod (2001) was conceived in under a year — Jobs gave Tony Fadell 8 months from concept to launch.
13. Jobs was Apple's CEO at a salary of $1/year from 1997 until his death in 2011, taking compensation in stock instead.
14. He insisted the Apple II computer be sold in retail electronics stores at a time when computers were sold through dealers and mail-order — a radical retail-channel bet.
15. The iconic Apple "Think Different" campaign (1997) was launched within months of Jobs returning to Apple as interim CEO; his first move was killing 70% of the product line.
16. Jobs personally rejected the original iPad keyboard design as "too small for fingers" and demanded the engineers shrink the bezel and increase the key size.
17. He coined the term "bicycle for the mind" to describe the personal computer, referencing a study that ranked the bicycle as the most efficient mode of locomotion among species.
18. The original Macintosh team signed the inside of the case — Jobs insisted, calling the Mac "a work of art" deserving of artists' signatures, even though no customer would ever see them.
19. Jobs was 56 when he died of pancreatic cancer on October 5, 2011 — just one day before Apple unveiled the iPhone 4S, the last product launch he approved.
20. He was estimated to be worth $10.2B at his death — but Apple stock split-adjusted from 1980's IPO would have made him significantly wealthier had he not sold most of his Apple stake after being ousted in 1985.

## Notes

- Authored from Telegram on 2026-05-20.
- Edit by saying `/refine steve-jobs-facts` in Telegram, or hand-edit this file in Claude Code.
- See `brain/skills/user-skills/steve-jobs-facts.md` for the original elicit Q&A.
