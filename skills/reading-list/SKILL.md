---
name: reading-list
version: 2
script: infra/reading-list.py
mutating: true
cron: null
eval: skills/reading-list/eval.yaml
last_audited: 2026-05-11
description: |
  Bookmark manager. The user wants to queue a URL for later, see what's
  queued, mark something as read, or drop something. Every saved URL is
  ingested deep — we always write a dedicated brain page for it so the
  list view can show a real one-line description, and the brain stays
  searchable. There is no "shallow" mode anymore.
  WHEN: Any of "save this", "save for later", "read later", "add to reading
  list", "queue this", "bookmark this", "show reading list", "what's
  queued", "show bookmarks", "I read X", "mark X as read", "finished X",
  "remove X", "drop X".
  NOT WHEN: The user wants the content absorbed RIGHT NOW under a different
  framing ("ingest this thread", "absorb this paper") — those route
  directly to ingest-x-thread / ingest-pdf. NOT for image OCR (→ image-ocr).
trigger: |
  - "save this", "save for later", "read later", "bookmark this"
  - "add to reading list", "queue this", "queue for reading"
  - "show reading list", "what's on my reading list", "reading queue"
  - "show bookmarks", "show my bookmarks", "what bookmarks do I have"
  - "I read X" / "mark X as read" / "I finished X"
  - "remove X from reading list" / "drop X"
writes_to:
  - lists/to-read
  - media/social/
  - media/youtube/
  - sources/
brain_dirs_read:
  - lists/to-read
trigger_guard: any_url
---

# /reading-list

Single source of truth: the `lists/to-read` brain page. All operations
go through `~/bin/reading-list.py` — never edit the page directly, never
improvise. The script handles brain page rebuild + git commit so data
lands in disk + git + R2 backup.

## Deep-by-default

Every `add` runs the right ingestor for the URL's kind and writes a
dedicated brain page. The row in `lists/to-read` carries the metadata
needed to render the list view without re-fetching anything.

| URL kind | Ingestor | Brain page slug pattern |
|---|---|---|
| X / Twitter status | `infra/ingest-x-thread.py` | `media/social/x-<handle>-<YYYYMMDD>` |
| YouTube video | `infra/ingest-youtube.py` | `media/youtube/<channel>-<video_id>` |
| PDF | `infra/ingest-pdf.py` | `sources/<slug>` |
| HTML (everything else) | `infra/ingest-html.py` (uses agent-browser) | `sources/<domain>-<slug>` |

If an ingestor fails, the row is still saved with the fields we have
(title from `<title>` / oEmbed, `posted_by` from URL host or handle,
`one_liner` left empty) so the queue never silently drops a save. The
user is told the brain page is missing so they can retry.

## Output contract — `show` (Telegram)

Each entry renders as three or four lines, **all at column 0** (no
indentation), with a blank line between entries:

```
1. <title in plain text — never wrapped in [..](..)>
Posted by: <handle, channel name, or domain>
About: <one-line description from the brain page>     ← omitted if blank
<single canonical URL — no t.co, no tracking params>
```

Invariants (asserted by `tests/test_reading_list_render.py`):

- Exactly **one URL** per entry, and it is the canonical URL.
- Titles are plain text. No `[Title](url)` markdown links.
- No `t.co/...` shortlinks anywhere in the rendered output.
- No tracking query params (`?si=...`, `?utm_*`, `?ref=...`).
- "Posted by:" is always present (falls back to URL host).
- One-line description is ≤ 200 chars, single line, no trailing URLs.
- **No line has leading whitespace.** Some Telegram clients and agent
  reply pipelines strip indentation; we use explicit "Posted by:" /
  "About:" labels and a blank-line separator instead.

`Recently read` uses the same block per entry with a leading `✓`
instead of `N.`.

## Row schema on `lists/to-read`

Queue:

```
| # | Title | Posted by | One-liner | Kind | URL | Added | Brain page |
```

`Recently read` adds a `Read` column between `Added` and `Brain page`.
`Title`, `Posted by`, and `One-liner` are stored as plain text — no
embedded markdown. The script never trusts the page to be hand-edited
and rewrites it in full on every mutation.

## Commands

```bash
# Add — always deep. Ingestor chosen from URL kind.
python3 ~/bin/reading-list.py add <url>

# Show queue + recent reads, formatted per the 4-line contract above.
python3 ~/bin/reading-list.py show

# Mark as read (by row number from `show`, or by URL/slug)
python3 ~/bin/reading-list.py read <n|url|slug>

# Remove from queue (same arg shape)
python3 ~/bin/reading-list.py drop <n|url|slug>

# Migrate older rows (v1 shallow / v1 deep) to v2 schema. Idempotent.
# Re-runs the right ingestor for any row missing a brain page or
# missing posted_by / one_liner.
python3 ~/bin/reading-list.py migrate
```

Every command prints a single-line JSON envelope on stdout with `ok`,
`user_message`, and operation-specific fields. Diagnostic logs go to
stderr.

## Canonical query path

Always invoke `python3 ~/bin/reading-list.py show`. Do NOT use:
- `gbrain query "reading list"` — page isn't in the embeddings index
  for that query shape
- `gbrain search "reading"` — surfaces noise
- `gbrain list` + grep — non-deterministic, depends on sync state

The script reads the canonical brain page directly via `gbrain get
lists/to-read`, parses the table, and renders for Telegram. Same
result every time.

## Resolver patterns

| Phrase pattern | Command |
|---|---|
| URL + ("save this" / "read later" / "queue this" / "bookmark this") | `add <url>` |
| "show reading list" / "show bookmarks" / "what's queued" / "what should I read" | `show` |
| "I read #2" / "mark #2 as read" / "finished #2" | `read 2` |
| "I read https://..." / "mark this as read: <url>" | `read <url>` |
| "drop #2" / "remove #2 from reading list" | `drop 2` |

When showing the list, ALWAYS use the numbered output so the user can
say "I read #2" without remembering slugs.

## Telegram reply

Relay the script's `user_message` field verbatim. Do NOT add diagnostic
info (cost, character count, processing time, brain DB stats).

## Architectural rules

Per `identity/CONTENT_INGESTION.md`:
- Every bookmark has a brain page (deep). No shallow path.
- "I read X" never deletes the brain page — only flips the row from
  Queue to Recently read.
- The list view never re-fetches from the network — `posted_by` and
  `one_liner` live on the row so `show` is fast and offline-safe.
