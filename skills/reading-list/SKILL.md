---
name: reading-list
version: 1
script: infra/reading-list.py
mutating: true
cron: null
eval: skills/reading-list/eval.yaml
last_audited: 2026-05-08
description: |
  Reading-list manager. The user wants to queue a URL for later, see what's
  queued, mark something as read, or drop something. Default is shallow
  (just title + URL row); explicit "absorb / deep" upgrades to full ingest.
  WHEN: Any of "save this", "save for later", "read later", "add to reading
  list", "queue this", "show reading list", "what's queued", "I read X",
  "mark X as read", "finished X", "remove X", "drop X".
  NOT WHEN: User explicitly wants the content absorbed into the brain right
  now ("ingest", "absorb", "remember this") — that's ingest-pdf or
  ingest-x-thread directly. NOT WHEN they want to extract text from an image
  (→ image-ocr).
trigger: |
  - "save this", "save for later", "read later"
  - "add to reading list", "queue this", "queue for reading"
  - "save this and absorb it" / "save this deep" → triggers deep ingest
  - "show reading list", "what's on my reading list", "reading queue"
  - "I read X" / "mark X as read" / "I finished X"
  - "remove X from reading list" / "drop X"
writes_to:
  - lists/to-read
  - sources/
  - media/social/
brain_dirs_read:
  - lists/to-read
---

# /reading-list

Single source of truth: the `lists/to-read` brain page. All operations
go through `~/bin/reading-list.py` — never edit the page directly, never
improvise. The script handles the brain page rebuild + git commit so
data lands in disk + git + R2 backup.

## Lazy by default, eager on explicit ask

| User intent | What runs | Cost |
|---|---|---|
| "save this", "read later" | `add <url>` (shallow) | Just URL + title fetch (free for HTML, ~$0.005 for X) |
| "save this and absorb it", "save deep", "save and ingest" | `add <url> --deep` | Above + full ingestion (PDF / X / etc), ~$0.01–0.05 |

Shallow rows live as table rows on `lists/to-read`. Deep rows ALSO get
their own `sources/` or `media/social/` page and the row links to it.

## Commands

```bash
# Add — shallow (default)
python3 ~/bin/reading-list.py add <url>

# Add — deep (also runs ingest-pdf or ingest-x-thread)
python3 ~/bin/reading-list.py add <url> --deep

# Show queue + recent reads (numbered, Telegram-formatted)
python3 ~/bin/reading-list.py show

# Mark as read (by row number from `show`, or by URL/slug)
python3 ~/bin/reading-list.py read <n|url|slug>

# Remove from queue (same arg shape)
python3 ~/bin/reading-list.py drop <n|url|slug>

# One-shot migration for legacy rows missing title/kind
python3 ~/bin/reading-list.py backfill
```

Every command prints a single-line JSON envelope on stdout with
`ok`, `user_message`, and operation-specific fields. Diagnostic logs
go to stderr.

## Canonical query path

Always invoke `python3 ~/bin/reading-list.py show`. Do NOT use:
- `gbrain query "reading list"` — returns no results (page isn't in
  the embeddings index for that query shape)
- `gbrain search "reading"` — surfaces noise
- `gbrain list` + grep — non-deterministic, depends on sync state

The script reads the canonical brain page directly via `gbrain get
lists/to-read`, parses the table, and renders for Telegram. Same
result every time.

## Resolver patterns

User says → run:

| Phrase pattern | Command |
|---|---|
| URL + ("save this" / "read later" / "queue this") | `add <url>` |
| URL + ("absorb it" / "and ingest" / "save deep") | `add <url> --deep` |
| "show reading list" / "what's queued" / "what should I read" | `show` |
| "I read #2" / "mark #2 as read" / "finished #2" | `read 2` |
| "I read https://..." / "mark this as read: <url>" | `read <url>` |
| "drop #2" / "remove #2 from reading list" | `drop 2` |

When showing a list, ALWAYS use the numbered output so the user can
say "I read #2" without having to remember slugs.

## Telegram reply

Relay the script's `user_message` field verbatim. Do NOT add diagnostic
info (cost, character count, processing time, brain DB stats).

## Architectural rules

Per `identity/CONTENT_INGESTION.md`:
- Shallow rows = title + URL only, lives only in `lists/to-read`
- Deep rows = full brain page (sources/ or media/social/) + row in
  `lists/to-read` linking to the page
- "I read X" never deletes the underlying brain page (if any) — only
  flips the reading-list row from Queue to Recently Read
