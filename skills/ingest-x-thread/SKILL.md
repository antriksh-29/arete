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

## Fetch order (2026-05-13)

X.com is auth-walled to unauthenticated clients — the public API
silently drops media-led tweets and unauth browser visits land on
the login wall. The script's fetch order is:

1. **browser-harness** with x.com cookies injected (if stored via
   `/site-login` → `SITE_COOKIES__x_com` in the vault). Gets the
   rendered tweet body + image alt-text + visible replies. This is
   the canonical path — most ingests should succeed here.
2. **X API** as fallback for the metadata (handle, name, date) when
   the browser path succeeds but is missing structured fields, OR
   as the full content source when the browser hits a login wall
   or isn't installed.
3. **Stub brain page** when both paths fail (no cookies, no token,
   chrome down). Writes a minimal `media/social/x-<handle>-<date>.md`
   that links to the tweet + documents the cookie-import next step.
   The script ALWAYS emits `ok: true` for a well-formed tweet URL —
   the agent never gets an "ingest failed" envelope to improvise on.

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

`X_API_BEARER_TOKEN` must be in the arete-cred vault. If absent:
```
arete-cred set X_API_BEARER_TOKEN <token>
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
