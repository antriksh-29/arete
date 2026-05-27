# Content ingestion principle

> **Every piece of media the user shares becomes a permanent brain page
> with our own analysis layered on top, and every mentioned entity gets
> a back-link plus a timeline entry.**

This is the media equivalent of `BRAIN_PROPAGATION.md`. Where brain
propagation governs entry points that learn about *entities*, content
ingestion governs entry points that absorb *long-form material* —
documents, threads, eventually videos and audio.

Per Garry's `docs/guides/content-media.md`, the goal is the same as
the rest of the brain system: media becomes searchable forever, with
the agent's commentary layered on top. Without this, links and PDFs
are bookmarks that decay.

---

## The three architectural rules

Apply to every content-ingestion path.

### Rule 1 — Never use auto-summaries

Full extracted text is the evidence base. YouTube auto-summaries, X
post AI summaries, "AI overview" features — all destroy the texture
that makes the source useful: who said what, exact phrasing, what was
left unsaid. Extract the raw material first, write our analysis on top.

### Rule 2 — Our own analysis is the value

Not a summary. Not regurgitation. The analysis must connect the new
material to what's already in the brain. The user's frame is what
makes a brain page valuable to him — generic summaries are worthless.

**Bad:** "The article discusses how startups should focus on
distribution."

**Good:** "Pollack's distribution argument lines up with what Naval has
been saying for years (see [[people/naval-ravikant]]) but extends it
with a specific tactic for B2B SaaS that contradicts the playbook
the user used at Arete (see [[companies/arete]] timeline 2024-Q3).
Worth revisiting before the next pricing decision."

The bad version is a summary anyone could write. The good version
requires reading the brain first — which is exactly the point.

### Rule 3 — Cross-references make pages alive

Every person, company, project, or concept mentioned in the ingested
material gets:
1. A `[[wikilink]]` in the analysis
2. A `gbrain link` edge (both directions)
3. A `gbrain timeline-add` entry on the entity's own page referencing
   the new media with one-line context

A media page without back-links is a dead archive. The back-links are
how the brain stays composable — querying a person's page surfaces
every piece of media they showed up in.

---

## Brain namespace conventions

| Media type | Page slug pattern | Status |
|---|---|---|
| PDF / book / long doc | `sources/<slug>.md` | Phase 1 (built) |
| X thread | `media/social/x-<handle>-<YYYYMMDD>.md` | Phase 1 (built) |
| YouTube video | `media/youtube/<channel>-<video-id>.md` | Phase 1 (built) |
| Generic HTML article | `sources/<domain>-<slug>.md` | Phase 1 (built) |
| Other social (LinkedIn, Threads, etc.) | `media/social/<platform>-<author>-<date>.md` | Deferred |
| Voice notes | `media/voice/<date>-<topic>.md` | Deferred |
| Forwarded WhatsApp / iMessage | `media/forwarded/<date>-<sender>.md` | Deferred |

`sources/` predates this doc and was Garry's convention for books and
long-form. `media/` is for ephemeral content (a tweet, a video) where
the source itself isn't a publication. The split is mostly cosmetic —
both follow identical analysis + cross-reference rules.

---

## Router pattern

The agent's URL/attachment router sits in `AGENTS.md` and dispatches
to the right ingestion skill based on URL shape and intent words.

Intent words that activate ingestion: `ingest`, `absorb`, `read this`,
`remember this`, `what does this say`, `worth reading`, `take notes`.

| URL/attachment shape | Skill |
|---|---|
| `*.pdf` URL or PDF Telegram attachment | `/ingest-pdf` |
| `twitter.com/*` or `x.com/*` | `/ingest-x-thread` |
| `youtube.com/*` or `youtu.be/*` | `/ingest-youtube` |
| Other HTML article URL | `/ingest-html` (static fetch); `/agent-browser` for JS-rendered / login-walled |

---

## Pre-ship checklist for new ingestion paths

When adding a new media type (e.g. when YouTube comes online):

- [ ] Does extraction yield raw text (or transcript), not an AI summary?
- [ ] Does the analysis prompt force the agent to reference brain context
      via `[[slug]]` syntax, not write a generic summary?
- [ ] Are entities cross-linked AND given timeline entries on their own
      pages? (per Rule 3)
- [ ] Is the brain page slug-stable so re-ingesting the same source
      updates the existing page rather than creating a duplicate?
- [ ] Does the user-facing Telegram reply stay tight (status → result),
      with no diagnostic noise (token counts, timing, costs)?
- [ ] Has the namespace pattern been added to the table above?
- [ ] Has the router rule been added to AGENTS.md?

---

## Scripts (Phase 1)

| Script | Purpose |
|---|---|
| `~/bin/ingest-pdf.py` | PDF URL or local path → analyzed brain page at `sources/<slug>.md` |
| `~/bin/ingest-x-thread.py` | X thread URL → analyzed brain page at `media/social/x-<handle>-<date>.md` |
| `~/bin/ingest-youtube.py` | YouTube URL → analyzed brain page at `media/youtube/<channel>-<video-id>.md` |
| `~/bin/ingest-html.py` | Generic HTML URL → analyzed brain page at `sources/<domain>-<slug>.md` |
| `~/bin/reading-list.py` | Bookmark manager — every `add` dispatches to one of the above and stores a row on `lists/to-read` |

All four ingestors write structured output the agent relays to Telegram in 4-6 lines.
Diagnostic information (page counts, token usage, API costs) goes to
stderr/logs only, never to the user.

---

## Why this doc exists

Same lever as `BRAIN_PROPAGATION.md` for the Gmail workflow: the
*architectural decisions* that make a system compounding need to be
captured in identity/, not re-derived per skill. New media types
slot into the framework above with no re-thinking.

### Bookmarks are deep, always

As of 2026-05-11, the `/reading-list` skill is **deep-by-default**: every
URL the user bookmarks runs through the right ingestor and gets its own
brain page. There is no "shallow row" path anymore. This is the
content-ingestion principle applied to the queue itself — a bookmark is
not a stub to revisit, it is a piece of media already absorbed, just
not yet read end-to-end by the user. When the user shares a media type we haven't seen
(audio file, screenshot, voice note), the agent reads this doc and
applies the same three rules to whatever extraction path it can
construct on the fly.
