---
name: ingest-x-thread
version: 1
script: infra/ingest-x-thread.py
mutating: true
cron: null
eval: skills/ingest-x-thread/eval.yaml
last_audited: 2026-05-08
description: |
  X (Twitter) thread ingestion. The user shares a tweet URL and wants the
  full bundle (canonical thread, quoted/referenced tweets, notable replies,
  linked articles) absorbed into the brain as a media/social page.
  WHEN: twitter.com/* or x.com/* URL + intent words like "ingest", "absorb",
  "read this", "remember this", "worth reading", "take notes".
  NOT WHEN: User wants the thread queued for later (→ reading-list shallow),
  or wants to draft a reply/quote-tweet (→ x-draft), or it's a non-X URL
  (→ ingest-pdf for PDFs, reading-list for general web).
trigger: |
  twitter.com/* or x.com/* URL plus any of:
  - "ingest", "absorb"
  - "read this", "remember this"
  - "what does this say"
  - "worth reading", "take notes"
writes_to:
  - media/social/
  - people/
  - companies/
  - concepts/
brain_dirs_read:
  - people/
  - companies/
  - concepts/
trigger_guard: x_url
---

# /ingest-x-thread

Take an X tweet URL, reconstruct the full bundle (canonical thread by the
same author, quoted/referenced tweets, notable replies, linked articles),
write our own analysis to the brain as `media/social/x-<handle>-<date>.md`,
and cross-link every mentioned entity per
`identity/CONTENT_INGESTION.md`.

## How to invoke

```
python3 ~/bin/ingest-x-thread.py <tweet-url>
```

Works with both `twitter.com/...` and `x.com/...` URLs. The script
extracts the tweet ID from the URL's `/status/<id>` segment.

The script prints a single-line JSON result on stdout. Diagnostic logs
go to stderr.

## Result envelope (stdout JSON)

Success:
```json
{
  "ok": true,
  "handle": "...",
  "name": "...",
  "thread_length": 7,
  "referenced_count": 1,
  "notable_replies": 2,
  "slug": "media/social/x-...",
  "linked_entities": [...],
  "insight_one_liner": "..."
}
```

Failure modes (relay `user_message` verbatim):
- `reason: "tweet_unavailable"` → tweet deleted/private/restricted
- `reason: "rate_limit"` → X API rate limit hit
- `reason: "auth"` → bearer token invalid
- `reason: "error"` → tell user something went wrong

## Pre-flight

`X_API_BEARER_TOKEN` must be in the bot-cred vault. If absent:
```
bot-cred set X_API_BEARER_TOKEN <token>
```
Get the token from console.x.com (pay-per-use, ~$0.005/post).

## Telegram reply format

Land 4-6 lines, no diagnostic noise:

```
🐦 Ingested thread: @{handle} — {topic from title}
Brain page: {slug}
{thread_length} tweets · {referenced_count} quoted · {notable_replies} notable replies
Linked entities: {first 5 entities, "+N more" if longer}
{insight_one_liner}
```

DO NOT mention: API costs, rate-limit budget remaining, GPT tokens.

## Architectural rules

Per `identity/CONTENT_INGESTION.md`:
1. Never use auto-summaries — the verbatim thread is preserved in the
   "Raw thread" section as the evidence base.
2. Our own analysis is the value — connect to existing brain context via
   `[[slug]]`, don't write a generic summary.
3. Cross-references make pages alive — every mentioned entity gets a
   back-link AND a timeline entry on its own page.

The script enforces all three. Don't bypass it.
