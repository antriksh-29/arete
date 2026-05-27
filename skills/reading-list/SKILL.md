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

## HARD ROUTING RULE — save/bookmark intent NEVER triggers /site-login

When the user pairs ANY URL with a save / bookmark / read-later /
"go through this article" / "summarize and store" / "add to reading
list" / "store it in brain" intent — the ONLY correct first action is
`python3 ~/bin/reading-list.py add <url>`. Even if the URL is on a
known auth-walled domain (x.com, linkedin.com, paid substack), even if
prior turns involved /site-login, the agent does NOT offer cookie
import as the response to a save intent. The ingestor decides if/when
cookies are needed: for X URLs it routes through browser-harness with
stored cookies if present, and writes a stub brain page documenting
the cookie-import next step if not.

Specifically banned first-response patterns when the user sends
`<URL> + save/bookmark/summarize`:

- *"🔒 Cookie import for <domain> — no extension required..."* — that
  is the /site-login flow, only triggered by an explicit "login to X"
  intent, never as a reaction to a save intent.
- *"I can add it to your reading list. Paste the tweet text or a
  screenshot, and I'll do the summary + brain note from that."* — no.
  Run the script. It handles thin tweets via browser-harness or by
  writing a stub brain page.
- *"This requires authentication, so..."* — irrelevant. Run the
  script. It writes a brain page either way.
- Asking the user to paste the tweet content — the script fetches the
  content. The user already sent a URL; that's the input.

If reading-list.py's envelope itself returns `reason: cookies_required`
in `user_message`, relay THAT verbatim. The script decides when
cookies are needed; the agent never decides on its own.

## Async ingest with follow-up message

`add <url>` is **ack-and-spawn**. The script:

1. Writes a placeholder row to `lists/to-read` with shallow metadata
   (handle for X, host for HTML, `(saving — brain page in progress…)`
   as the title).
2. Forks `reading-list-worker.py` as a detached background process.
3. Returns an envelope to the agent within a second with
   `pending: true` and a `user_message` like:

   ```
   📥 Saving to bookmarks: @tobi
   https://x.com/tobi/status/...

   Fetching the article and writing the brain page now — I'll send a
   follow-up when it's ready (usually <2 min).
   ```

The worker runs the deep ingestor (which may take 60–200s for
media-led X tweets that need a browser-harness fallback), updates the
placeholder row in place, and posts a second Telegram message via
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_USER_ID` from vault:

```
✅ Bookmark ready: <title>
Posted by: <handle>
About: <one-liner>
<canonical url>

Brain page: media/social/x-...
```

Why this exists: prior to 2026-05-13 the agent ran the deep ingest
inside the Telegram turn. Thin X tweets (image/video/link-only) hit
the browser-harness fallback path, total runtime exceeded the 120s
per-turn budget documented in `docs/E2E-FINDINGS.md`, and the bot
returned an "ingest didn't finish, got terminated" apology. The
async path keeps the turn under 5s while still delivering the full
brain page.

`READING_LIST_SYNC=1` reverts to the legacy synchronous flow for
smoke tests and CI (the worker would otherwise return before the
test asserts).

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

## Deferred entity cross-references

Each ingestor (`ingest-x-thread.py`, `ingest-youtube.py`,
`ingest-html.py`) used to call `gbrain link` + `gbrain timeline-add`
inline for every `[[wikilink]]` in the freshly-written brain page —
2-3 subprocess calls per entity, up to ~30s for a 10-entity page.

Now they append the entity batch to `{{user.brain_path}}/.xref-queue.jsonl`
and return. `xref-drainer.py --once` reads + clears the queue every
60s via `xref-drainer.timer`, plus the reading-list worker kicks the
drainer one-shot at the end of its run so new bookmarks see their
entity links populate within seconds of the follow-up message.

Failure mode if the timer + worker are both unavailable: the queue
file grows; entity links stay missing until the next `migrate
bookmarks` or a manual `python3 ~/bin/xref-drainer.py --once`.

## Architectural rules

Per `identity/CONTENT_INGESTION.md`:
- Every bookmark has a brain page (deep). No shallow path.
- "I read X" never deletes the brain page — only flips the row from
  Queue to Recently read.
- The list view never re-fetches from the network — `posted_by` and
  `one_liner` live on the row so `show` is fast and offline-safe.
