---
name: brain-graph
version: 1
script: infra/brain-graph.py
mutating: false
cron: null
eval: skills/brain-graph/eval.yaml
last_audited: 2026-05-14
description: |
  Walk the [[wikilink]] graph from a brain page. Returns forward
  links (entities the page references) AND backlinks (other pages
  that reference this one). Pure file walking — no LLM, no gbrain.
  WHEN: "what connects to <X>" / "what else mentions <X>" / "show
  the graph around <X>" / "what links to Nishank's article".
  NOT WHEN: User wants the content of one page (→ brain-ask), or to
  compare two pages (→ brain-compare).
trigger: |
  - "what connects to <X>" / "what links to <X>"
  - "what else mentions <X>"
  - "show the graph around <X>" / "show backlinks for <X>"
writes_to: []
brain_dirs_read:
  - all (recursive scan of *.md)
trigger_guard: free_text
---

# /brain-graph

Two-way edge walker. Forward edges come from the source page's body
(every `[[target]]` reference). Backlinks come from an O(N) scan of
all `.md` files in `{{user.brain_path}}` — fine at the current scale, swap to
gbrain index if the brain ever grows past ~5k pages.

`--depth 2` follows forward links one more hop (backlink-of-backlink
balloons fast and isn't useful). Pointer accepts the same shapes as
brain-ask: slug | "last" | URL | free-text via brain-find.

Output: grouped by slug prefix (people/ vs concepts/ vs companies/
vs notes/ etc.) so the Telegram render is scannable.
