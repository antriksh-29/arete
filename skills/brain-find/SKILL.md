---
name: brain-find
version: 1
script: infra/brain-find.py
mutating: false
cron: null
eval: skills/brain-find/eval.yaml
last_audited: 2026-05-14
description: |
  Free-text → brain page slug resolver. Internal helper called by
  brain-ask / brain-compare / brain-graph / brain-edit / note --source
  when their input is a keyword reference like "Nishank's article".
  Scores every row in lists/to-read by handle / posted_by / title /
  URL host / slug; falls back to gbrain search if lexical match is
  empty. Returns ranked candidates + confidence marker.
  WHEN: Almost always called internally by other brain-* skills, not
  by the agent directly. The agent only invokes brain-find on its own
  when it needs to disambiguate two candidates ("did you mean A or B?")
  before running a follow-up tool call.
  NOT WHEN: User wants a topical search with surfaced results
  (→ brain-search — same scoring, but presents results to the user
  with one_liners + numbered list).
trigger: |
  - (internal — used by other Layer 3 scripts)
  - Direct: "which brain page matches <phrase>" / "did I bookmark
    something by <author>" — rare; usually brain-search is what the
    user wants.
writes_to: []
brain_dirs_read:
  - lists/to-read
trigger_guard: free_text
---

# /brain-find

Two-tier resolver:

1. **Lexical match** against `lists/to-read` rows. Stop words and
   decorations ("article", "please", "top three", etc.) are
   filtered so common decoration in the user's question doesn't
   accidentally match everything. Scores:
   - exact handle match → 1.0
   - handle substring → 0.85
   - posted_by substring → 0.7
   - title substring → 0.5
   - URL host / slug substring → 0.4

2. **Semantic fallback** via `gbrain search "<query>"` when nothing
   scores ≥ 0.4 lexically.

Returns `{ok, best, confident, candidates[]}` — `confident=True`
when the top score is ≥ 1.5× the second-place score, OR there's
only one candidate. Confidence drives whether the calling script
proceeds directly or surfaces a "did you mean?" prompt.

## Why this exists

Before brain-find (2026-05-14), every brain-* skill defaulted to
the `last` pointer when the user named an author or topic. That
pointed to whichever row was most recently hydrated, which drifted
during migrate and produced confidently-wrong answers (the v100
"Nishank" → QOVES misfire). Resolving by name first is the
correctness fix.
