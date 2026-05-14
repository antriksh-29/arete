---
name: brain-search
version: 1
script: infra/brain-search.py
mutating: false
cron: null
eval: skills/brain-search/eval.yaml
last_audited: 2026-05-14
description: |
  Discovery / retrieval across the brain. "What's in my brain about
  ambition?" / "find me everything on founder-mode" / "what was I
  reading about persistence last week" — pure retrieval, no LLM call.
  WHEN: User asks for a topical search across saved bookmarks + notes.
  NOT WHEN: User names a single article (→ brain-ask), or compares
  two specific things (→ brain-compare), or wants to walk relationships
  from a known page (→ brain-graph).
trigger: |
  - "find articles about <X>" / "find everything on <X>"
  - "what's in my brain about <X>" / "search for <X>"
  - "anything I saved on <X>"
  - "what was I reading about <X> last week" (use --since)
writes_to: []
brain_dirs_read:
  - lists/to-read
  - notes/
trigger_guard: free_text
---

# /brain-search

Lexical match across `lists/to-read` rows + on-disk `notes/`. Falls
back to `gbrain search` for semantic queries when nothing matches
lexically. Returns ranked results with title / posted_by / one_liner /
slug.

Each result is one bookmark or note. The agent relays the
`user_message` (a numbered Telegram-friendly list) and the user can
follow up with `brain-ask` / `brain-compare` / `brain-graph` against
any of the returned slugs.

Flags: `--limit N` (default 5), `--kind <x_thread|html|note|...>`,
`--since YYYY-MM-DD`.
