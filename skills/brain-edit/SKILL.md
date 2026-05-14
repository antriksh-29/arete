---
name: brain-edit
version: 1
script: infra/brain-edit.py
mutating: true
cron: null
eval: skills/brain-edit/eval.yaml
last_audited: 2026-05-14
description: |
  Surgical edits to an existing brain page. Three modes — add a
  [[wikilink]] to Connections, append content, or update a
  frontmatter field. Diff-safe (preserves frontmatter + Connections
  section).
  WHEN: "update <slug> — add a link to [[X]]" / "append to <slug>:
  ..." / "set <slug>'s field to value" / "mark <slug> as read".
  NOT WHEN: User wants to bookmark a new URL (→ reading-list add),
  or save a fresh note (→ note.py).
trigger: |
  - "update <slug> — add a link to <X>"
  - "add [[X]] to <slug>" / "connect <slug> to [[X]]"
  - "append to <slug>: <content>" / "add this section to <slug>: ..."
  - "set <slug>'s <field> to <value>"
writes_to:
  - "<slug>"        # the edited page
  - .xref-queue.jsonl
brain_dirs_read:
  - "<slug>"
trigger_guard: free_text
---

# /brain-edit

Three subcommands:

| Subcommand | Effect |
|---|---|
| `add-link <pointer> <entity> [reason]` | Adds `[[entity]]` under `## Connections` (creates section if missing, dedupes). Enqueues a cross-ref via xref-drainer so the entity page backlinks. |
| `append <pointer> <content>` | Appends `<content>` to the page body, inserting ABOVE any existing trailing Connections section so its location stays stable. Picks up any `[[wikilinks]]` in the appended text for xref. |
| `set-field <pointer> <field> <value>` | Updates (or creates) a frontmatter field. Useful for status flags, custom metadata, etc. |

Every mode also refreshes the `updated:` frontmatter to today's date.

Pointer accepts: slug | "last" | URL | free-text via brain-find.
Citation `📎 source: <slug>` is appended to every `user_message`.
