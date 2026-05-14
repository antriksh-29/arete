---
name: note
version: 1
script: infra/note.py
mutating: true
cron: null
eval: skills/note/eval.yaml
last_audited: 2026-05-14
description: |
  Capture Antriksh's own thoughts, reactions, reminders, and observations
  into the brain as first-class pages under notes/. The brain stops
  being a passive collection of ingested bookmarks and becomes a
  two-way store — Antriksh's voice lands alongside the sources he
  reacts to.
  WHEN: Any of "take a note", "save this thought", "note to self",
  "remember that", "my reaction to", "add to brain", "append to my note",
  "add to my note about".
  NOT WHEN: The user is saving a URL (→ reading-list add), ingesting
  someone else's content (→ ingest-x-thread / ingest-html), or asking
  a question about existing content (→ brain-ask).
trigger: |
  - "take a note: <text>" / "save this thought: <text>"
  - "note to self: <text>" / "remember that <text>"
  - "my reaction to <X>: <text>" / "add to brain on <topic>: <text>"
  - "append to my note on <topic>: <text>"
writes_to:
  - notes/
  - people/
  - companies/
  - concepts/
brain_dirs_read:
  - people/
  - companies/
  - concepts/
trigger_guard: free_text
---

# /note

Antriksh's own thoughts, in his own voice, written to the brain so
brain-find can route to them and brain-ask can quote from them. Notes
compound with bookmarks — once you've reacted to a Nishank thread, a
future "what do I think about ambition" query surfaces both Nishank's
page and your note.

## Modes

| Mode | When | Command |
|---|---|---|
| Fresh note | Standalone thought | `note.py "<text>"` |
| Note linked to a source | Reaction to a bookmark | `note.py "<text>" --source <slug>` |
| Append to existing | Extending an earlier note | `note.py "<text>" --append-to notes/<slug>` |

The script auto-derives the slug from the first sentence (≤80 chars,
slugified). Collisions get `-2`, `-3` suffixes.

## Page shape

```markdown
---
type: note
created: 2026-05-14
updated: 2026-05-14
author: antriksh
source: media/social/x-nishank-20260512   # optional
---

# <auto-derived title from first sentence>

<the note body, verbatim — never paraphrased>

## Source
- [[media/social/x-nishank-20260512]]

## Connections
- [[concepts/founder-mode]] — mentioned in the note text
- [[people/nishank-singla]] — mentioned in the note text
```

## Verbatim rule

Notes are Antriksh's voice. The script writes the body **exactly** as
typed. The agent does NOT clean up phrasing, expand contractions,
"clarify" wording, or wrap in framing like "The user notes that…".
If Antriksh writes "Nishank is wrong about X", the brain page body
is "Nishank is wrong about X" — not "The user disagrees with Nishank
about X". This is consistent with how ingested content preserves
exact phrasing in `## Raw thread` / raw sidecars.

## Auto-link entities

After the body is written, `note.py` walks `~/.brain/{people,companies,concepts}/`
and adds `[[wikilinks]]` for every existing entity slug the body mentions
by name. Explicit `[[wikilinks]]` typed by the user are preserved as-is.
All links go through the same `~/.brain/.xref-queue.jsonl` that
`ingest-x-thread` uses, so `xref-drainer` populates entity stubs +
backlinks asynchronously within ~60s.

## Output contract

JSON envelope on stdout with at minimum:
- `ok` — bool
- `slug` — the note's brain page slug (e.g. `notes/2026-05-14-nishank-survivorship-stage-asymmetry`)
- `user_message` — the Telegram-relayable reply, ending with `📎 source: <slug>` so future questions can navigate back

The trailing `📎 source: <slug>` line is part of `user_message` and
must NOT be stripped on relay (same rule as brain-ask).
