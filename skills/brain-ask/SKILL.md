---
name: brain-ask
version: 2
script: infra/brain-ask.py
mutating: false
cron: null
eval: skills/brain-ask/eval.yaml
last_audited: 2026-05-14
description: |
  Answer a question about a SINGLE bookmarked / noted brain page.
  Loads the brain page body + verbatim raw sidecar (when present),
  asks the LLM with a "quote from the source, cite the slug" prompt.
  Optional --persona flag injects a reader frame (defaults to
  people/antriksh) so the answer is calibrated to who's asking.
  WHEN: User asks a content question that names a specific source —
  "what does Nishank say about X", "quote the top 3 takeaways",
  "summarize that article", "explain Nishank's thesis in my context".
  NOT WHEN: User wants a topical search across many pages (→
  brain-search), or to compare 2+ pages (→ brain-compare), or to
  walk the graph from a page (→ brain-graph), or to add a note
  (→ note).
trigger: |
  - "what does <X> say about <Y>"
  - "quote <X>" / "give me the key takeaways from <X>"
  - "summarize <X>" / "what's the core argument of <X>"
  - "explain <X> in my context" (use --persona)
  - "tell me more about <X>" (after a brain-search result)
writes_to: []
brain_dirs_read:
  - "<slug>"        # the brain page being asked about
  - _raw/           # verbatim sidecar from browser-harness, if present
  - people/         # only when --persona is set (default people/antriksh)
trigger_guard: free_text
---

# /brain-ask

Single-source Q&A. Resolves the pointer via the same shapes every
Layer 3 script accepts:

- a literal slug (`media/social/x-nishank-20260512`)
- `last` — reads `~/.brain/.last-bookmark.json` (set by reading-list
  worker after each completed ingest). Only use for unambiguous
  deictic references ("that article", "this one").
- a URL (looked up in `lists/to-read`)
- free text — delegated to `~/bin/brain-find.py`, which scores
  every row in lists/to-read by handle / posted_by / title / URL
  host. This is the default for keyword references like "Nishank's
  article".

## --persona

Adds a `READER FRAME` block to the LLM prompt loaded from the
persona page (default `people/antriksh`). Use when the user asks
for an explanation calibrated to their context: "explain X to me
in my context", "given my background, what should I take from X",
"what does X mean for someone in my role". Without --persona the
prompt is unchanged — don't add it for plain Q&A.

## Output

JSON envelope on stdout with `user_message`. Relay verbatim. The
trailing `📎 source: <slug>` line is part of `user_message` — do
NOT strip it (RULE 0.7 byte-for-byte).
