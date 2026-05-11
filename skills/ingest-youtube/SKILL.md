---
name: ingest-youtube
version: 1
script: infra/ingest-youtube.py
mutating: true
cron: null
eval: skills/ingest-youtube/eval.yaml
last_audited: 2026-05-11
description: |
  YouTube video ingestion. The user shares a youtube.com or youtu.be URL
  and wants the video absorbed into the brain as a media/youtube page:
  channel + title + transcript (when available) + our own analysis
  layered on top per identity/CONTENT_INGESTION.md.
  WHEN: youtube.com/* or youtu.be/* URL + intent words like "ingest",
  "absorb", "remember this video", "take notes on this", or when called
  by /reading-list `add` for a YouTube URL.
  NOT WHEN: User wants to draft a tweet about a video (→ x-draft). NOT
  for non-YouTube URLs (→ ingest-x-thread for X, ingest-html otherwise).
trigger: |
  youtube.com/* or youtu.be/* URL plus any of:
  - "ingest", "absorb"
  - "remember this video", "take notes on this video"
  - "what does this video say"
  - "watch this for me" / "summarize this video"
writes_to:
  - media/youtube/
  - people/
  - companies/
  - concepts/
brain_dirs_read:
  - people/
  - companies/
  - concepts/
trigger_guard: youtube_url
---

# /ingest-youtube

Take a YouTube URL, fetch channel + title via oEmbed, pull the transcript
when available (via `youtube-transcript-api`, falling back to
`agent-browser` for description-only), write our own analysis to the
brain as `media/youtube/<channel-slug>-<video-id>.md`, and cross-link
mentioned entities per `identity/CONTENT_INGESTION.md`.

## How to invoke

```
python3 ~/bin/ingest-youtube.py <youtube-url>
```

Works with both `youtube.com/watch?v=...` and `youtu.be/...` URLs. The
script extracts the video ID, normalizes the URL (strips `?si=...`
tracking), and dispatches.

The script prints a single-line JSON result on stdout. Diagnostic logs
go to stderr.

## Result envelope (stdout JSON)

Success:
```json
{
  "ok": true,
  "video_id": "...",
  "channel": "...",
  "title": "...",
  "url": "https://www.youtube.com/watch?v=...",
  "slug": "media/youtube/...",
  "transcript_available": true,
  "linked_entities": [...],
  "insight_one_liner": "..."
}
```

Failure modes (relay `user_message` verbatim):
- `reason: "video_unavailable"` → video removed/private/age-restricted
- `reason: "transcript_unavailable"` → no captions; analysis from
  title + description only (still writes a brain page)
- `reason: "error"` → tell user something went wrong

## Pre-flight

`OPENAI_API_KEY` must be in the bot-cred vault. `youtube-transcript-api`
must be importable (`pip install youtube-transcript-api`). If the
transcript lib is missing, the script falls back to `agent-browser` to
pull the video's description and title block.

## Telegram reply format

Land 4-5 lines, no diagnostic noise:

```
📺 Ingested: {title} — {channel}
Brain page: {slug}
{insight_one_liner}
```

DO NOT mention: API costs, transcript provider, GPT tokens.

## Architectural rules

Per `identity/CONTENT_INGESTION.md`:
1. Never use auto-summaries — when a transcript is available, it goes
   into the brain page verbatim as the evidence base.
2. Our own analysis is the value — connect to existing brain context
   via `[[slug]]`, don't write a generic summary.
3. Cross-references make pages alive — every mentioned entity gets
   a back-link via `gbrain link` and a timeline entry.

The script enforces all three. Don't bypass it.
