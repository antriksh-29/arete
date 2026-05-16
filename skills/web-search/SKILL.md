---
name: web-search
version: 2
script: infra/web-search.sh
mutating: false
cron: null
eval: skills/web-search/eval.yaml
last_audited: 2026-05-16
description: |
  Live web search via browser-harness against DuckDuckGo HTML.
  Use when the user asks for news, current events, "latest X", current
  prices, or fresh research that requires sources. Brain-first per rule 9.
  WHEN: "latest <topic>", "current price of X", "what's happening with X",
  "news on Y", "search the web for Z", "find a source for X".
  NOT WHEN: answerable from brain (use brain-search first); calendar/
  Gmail data (use google-composio); trivial knowledge the model already
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

# /web-search — live web via browser-harness

When the user asks for live information from the internet (news, recent
events, "latest X", current prices, "what is X" for things not in the brain,
research that requires fresh sources):

**MUST RUN:** `bash ~/bin/web-search.sh "<the user's query, paraphrased tightly>"`

Do not apologize, do not say "I don't have web search" — call the script.
The script drives the always-on headless Chrome (bh-chrome.service) via
browser-harness against DuckDuckGo's HTML endpoint and returns a markdown
list of top 5 results: title, snippet, and direct URL.

Relay the markdown output verbatim, or add one sentence of your own framing.
Do NOT re-summarize aggressively — the page already filtered.

**Why browser-harness, not the model's built-in web tool:**
- Keeps web access on infra we own; no model-provider dependency.
- Works the same across any model swap in our routing chain.
- Already-running headless Chrome (~zero startup cost per call).

**When NOT to use web-search.sh:**
- Anything answerable from the brain (use `gbrain__query` first; brain-first
  per rule 9 of AGENTS.md)
- Anything answerable from your own knowledge for the relevant date window
  (model cutoff)
- Calendar / Gmail data — those go through composio MCP, not web search
