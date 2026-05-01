---
name: web-search
version: 1.0.0
description: |
  Live web search via OpenAI GPT-5.4-mini. Use when the user asks for news, current events, latest X, current prices, or fresh research that requires sources.
---

# web-search — live web via mini

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

