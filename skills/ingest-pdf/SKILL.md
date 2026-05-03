---
name: ingest-pdf
description: Ingest a PDF (URL or Telegram attachment) into the brain as an analyzed source page with cross-references to mentioned entities.
trigger: |
  PDF URL (ending .pdf) or PDF Telegram attachment, plus any of:
  - "ingest", "absorb"
  - "read this", "remember this"
  - "what does this say"
  - "worth reading", "take notes"
mutating: true
writes_to:
  - sources/
  - people/
  - companies/
  - concepts/
---

# /ingest-pdf

Take a PDF URL or local file, extract its text, write our own analysis to
the brain as `sources/<slug>.md`, and cross-link every mentioned entity
per `identity/CONTENT_INGESTION.md`.

## How to invoke

User shares a PDF (URL or Telegram attachment) with intent words above.

For a URL:
```
python3 ~/bin/ingest-pdf.py <url>
```

For a Telegram-attached PDF:
1. Pull the file via `clawvisor__telegram_get_file` or the bot API
   `getFile` → `https://api.telegram.org/file/bot<TOKEN>/<file_path>`,
   save to `/tmp/`.
2. Run:
```
python3 ~/bin/ingest-pdf.py /tmp/<file>.pdf
```

The script prints a single-line JSON result on stdout. Diagnostic logs
go to stderr.

## Result envelope (stdout JSON)

Success:
```json
{
  "ok": true,
  "title": "...",
  "author": "...",
  "slug": "sources/...",
  "linked_entities": ["people/...", "companies/...", ...],
  "insight_one_liner": "..."
}
```

Failure modes:
- `reason: "scanned_or_empty"` → relay `user_message` verbatim (OCR not wired)
- `reason: "error"` → log the error, tell user something went wrong, ask
  to try again

## Telegram reply format

Land 4-6 lines, no diagnostic noise:

```
📄 Ingested: {title} by {author}
Brain page: {slug}
Linked entities: {first 5 entities, "+N more" if longer}
{insight_one_liner}
```

DO NOT mention: page count, character count, GPT tokens, processing time,
API costs.

## Architectural rules

Per `identity/CONTENT_INGESTION.md`:
1. Never use auto-summaries — full extracted text is the evidence base.
2. Our own analysis is the value — connect to existing brain context via
   `[[slug]]`, don't write a generic summary.
3. Cross-references make pages alive — every mentioned entity gets a
   back-link AND a timeline entry on its own page.

The script enforces all three. Don't bypass it.
