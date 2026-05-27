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
  people/{{user.slug}}) so the answer is calibrated to who's asking.
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
  - people/         # only when --persona is set (default people/{{user.slug}})
trigger_guard: free_text
---

# /brain-ask

Single-source Q&A. Resolves the pointer via the same shapes every
Layer 3 script accepts:

- a literal slug (`media/social/x-nishank-20260512`)
- `last` — reads `{{user.brain_path}}/.last-bookmark.json` (set by reading-list
  worker after each completed ingest). Only use for unambiguous
  deictic references ("that article", "this one").
- a URL (looked up in `lists/to-read`)
- free text — delegated to `~/bin/brain-find.py`, which scores
  every row in lists/to-read by handle / posted_by / title / URL
  host. This is the default for keyword references like "Nishank's
  article".

## --persona

Adds a `READER FRAME` block to the LLM prompt loaded from the
persona page (default `people/{{user.slug}}`). Use when the user asks
for an explanation calibrated to their context: "explain X to me
in my context", "given my background, what should I take from X",
"what does X mean for someone in my role". Without --persona the
prompt is unchanged — don't add it for plain Q&A.

## Output

JSON envelope on stdout with `user_message`. Relay verbatim. The
trailing `📎 source: <slug>` line is part of `user_message` — do
NOT strip it (RULE 0.7 byte-for-byte).

## Disambiguation — search vs ask vs ask-persona

brain-search, brain-ask, and brain-ask --persona look superficially
similar (all touch brain content + return prose) but answer different
question shapes. A 2026-05-14 probe caught the agent running
`brain-ask --persona` for "find articles about ambition in my brain" —
that should have been brain-search. Hard distinction:

| User wants… | Means → | Route to |
|---|---|---|
| **A list of brain pages** about a topic ("find articles", "what's in my brain about X", "search for Y", "show me everything on Z") — answer is plural, multi-source | discovery | `brain-search.py` |
| **Quotes / takeaways / summary from ONE named source** ("what does Nishank say", "quote the takeaways", "summarize Tobi's thread") — answer is singular, source-anchored | single-page Q&A | `brain-ask.py` (no --persona) |
| **Calibrated explanation of ONE named source to the user's context** ("explain X in my context", "given my background, what should I take from X") — singular + reader-frame | persona-Q&A | `brain-ask.py --persona` |

**Banned first action:** routing a search intent (plural, multi-source)
to `brain-ask`. Even if the user says "find articles about Nishank's
view on ambition", the answer shape is "here are the matching brain
pages", not "here's a calibrated explanation of one of them". When in
doubt, brain-search; the result list lets the user follow up with
"tell me more about #2" which IS a brain-ask intent.

**Banned phrase patterns for `--persona`** when phrasing is plural:

- *"find articles about X in my brain"* → never persona-ask, always brain-search.
- *"what's in my brain about X"* → never persona-ask.
- *"what was I reading about X last week"* → `brain-search.py "X" --since YYYY-MM-DD`.

`--persona` is a single-source modifier. If you don't have a single
source in mind, you don't have a persona-ask.

## brain-ask works for ALL bookmark kinds

X tweets, YouTube videos, PDFs, HTML articles — they all have brain
pages and (when browser-harness ran successfully at ingest) raw
sidecars at `{{user.brain_path}}/_raw/<slug>.raw.txt`. Don't refuse with "I don't
have the article text" or "paste the thread text or a screenshot" —
brain-ask.py reads the raw sidecar; run it. Only if it returns
`ok: false` with `reason: not_found` may you ask the user for the
source.
