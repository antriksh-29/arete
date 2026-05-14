---
name: brain-compare
version: 1
script: infra/brain-compare.py
mutating: false
cron: null
eval: skills/brain-compare/eval.yaml
last_audited: 2026-05-14
description: |
  Compare two or more brain pages side-by-side with quotes. Loads
  page bodies + raw sidecars (when present), sends to LLM with a
  prompt that requires verbatim quotes and per-source citations.
  WHEN: "compare X and Y" / "what does A say vs B" / "contrast X's
  view on Y with Z's view".
  NOT WHEN: User wants a single source's content (→ brain-ask), or
  a topical search across many (→ brain-search).
trigger: |
  - "compare <X> and <Y>" / "compare <X> with <Y>"
  - "<A> vs <B> on <topic>" / "contrast <A> and <B>"
  - "what does <A> say vs what does <B> say"
writes_to: []
brain_dirs_read:
  - all (loads N pages by slug)
  - _raw/  (verbatim sidecars when present)
trigger_guard: free_text
---

# /brain-compare

Cross-page synthesis. Takes 2+ pointers (same shapes as brain-ask),
resolves them via brain-find when needed, loads each source's
brain page body + raw sidecar, hands all of it to the LLM with a
"compare and contrast, quote verbatim, cite per-source" prompt.

Returns the synthesis as continuous prose with inline `[[slug]]`
citations, plus a closing `📎 sources: slug-a, slug-b` line. Agent
relays user_message verbatim.

`--question "..."` provides a specific lens for the comparison
(e.g. "what do they each say about persistence"). Default behavior
without a question: shared ground → key divergence → which is
stronger and why.
