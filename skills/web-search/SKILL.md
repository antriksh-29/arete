---
name: web-search
version: 1
script: infra/web-search.sh
mutating: false
cron: null
eval: skills/web-search/eval.yaml
last_audited: 2026-05-16
description: |
  Live web search via OpenAI GPT-5.4-mini with native web_search_preview.
  Use when the user asks for news, current events, "latest X", current
  prices, or fresh research that requires sources. Brain-first per rule 9.
  WHEN: "latest <topic>", "current price of X", "what's happening with X",
  "news on Y", "search the web for Z", "find a source for X".
  NOT WHEN: answerable from brain (use brain-search first); calendar/
  Gmail data (use clawvisor-live); trivial knowledge the model already
  has within cutoff; user message lacks a query phrase.
trigger: |
  - "latest <topic>" / "current <metric>"
  - "news on <topic>" / "what's happening with <topic>"
  - "search the web for <query>"
  - "find me a source for <claim>"
  - "look up <thing>"
writes_to: []
brain_dirs_read: []
mutates_external_state: false
---

# /web-search — live web via mini

When the user asks for live information from the internet (news, recent
events, "latest X", current prices, "what is X" for things not in the brain,
research that requires fresh sources):

1. Call the bash tool with: `~/bin/web-search.sh "<the user's query, paraphrased tightly>"`
2. The script returns markdown synthesized by GPT-5.4-mini using OpenAI's
   web_search tool (cheap-tier per eng plan §14.1). It includes inline source
   URLs in parentheses — preserve those when relaying to the user.
3. Pass-through-friendly: the markdown is already concise. You can either
   forward it verbatim, or add one sentence of your own framing. Do NOT
   re-summarize aggressively — the synthesis pass already happened.

**When NOT to use web-search.sh:**
- Anything answerable from the brain (use `gbrain__query` first; brain-first
  per rule 9 of AGENTS.md)
- Anything answerable from your own knowledge for the relevant date window
  (model cutoff)
- Calendar / Gmail data — those go through clawvisor MCP, not web search

**Cost note:** ~$0.001-0.005 per call. Don't web-search trivial queries you
already know the answer to.
