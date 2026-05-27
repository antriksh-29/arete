# Brain filing rules

Single source of truth for **where new knowledge goes** in
`{{user.brain_repo}}`. Every skill that writes to the brain MUST
consult this document first. The audit (`infra/check-resolvable.mjs`)
validates each SKILL.md's `writes_to:` / `brain_dirs_read:` against the
canonical directory list below.

## Why this file exists

> "A skill is a routing table for context. When task type X appears,
> load skill Y first." — *The Routing Table for Intelligence*

The same logic applies one layer down: when knowledge type X arrives,
file it at directory Y. Without an explicit rule, two skills file the
same kind of fact in two different places, the brain becomes a junk
drawer, and gbrain queries return nothing because the page is in the
wrong neighbourhood.

## Canonical directories

Primary subject (not format, not source) determines the directory.

| Content | Dir | Slug pattern | Real example |
|---|---|---|---|
| Person | `people/` | `<lowercase-hyphenated>` | `people/{{user.slug}}.md`, `people/garry-tan.md` |
| Company / org / startup | `companies/` | `<lowercase-hyphenated>` | `companies/alexlance.md` |
| Concept / mental model / framework | `concepts/` | `<lowercase-hyphenated>` | `concepts/ai-agent.md`, `concepts/founder-mode.md` |
| Project (own work or close-watched) | `projects/` | `<lowercase-hyphenated>` | `projects/personal-ai-assistant.md` |
| Meeting notes | `meetings/` | `YYYY-MM-DD-<participants>-<topic>` | `meetings/2026-03-11-alice-bob-arete-discussion.md` |
| Habit / routine / recurring behaviour | `habits/` | `<lowercase-hyphenated>` | `habits/book-reading.md`, `habits/daily-tasks.md`, `habits/water-intake.md` |
| Curated list (state document, single per topic) | `lists/` | `<lowercase-hyphenated>` | `lists/to-read.md` |
| Social / consumed media (movies, shows, articles, threads) | `media/` or `media/social/` | `<topic>` or `x-<handle>-<YYYYMMDD>` | `media/bait.md`, `media/social/x-garrytan-20260408.md` |
| Raw ingestion corpus (verbatim author dumps) | `corpus/` | `<source>-<id>` | `corpus/x-garrytan.md`, `corpus/x-paulg.md` |
| Source documents (PDFs, papers, articles whose own analysis we wrote) | `sources/` | preserve source-title slug | `sources/published-as-a-conference-paper-at-iclr-2024.md` |

Anything NOT covered by this table goes in a triage queue at
`_inbox/<YYYY-MM-DD>-<slug>.md` and gets moved by the next run of
`gbrain dream extract` (or a human review). **Never invent a new
top-level dir without updating this file first.**

## Frontmatter convention (every page)

```yaml
---
type: <person|company|concept|project|meeting|habit|list|media|corpus|source>
name: "<human-readable title>"           # required for entities
aliases: ["<alt-1>", "<alt-2>"]          # optional
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: ["<tag-1>", "<tag-2>"]             # optional
---
```

`type:` MUST equal the directory name (singular). The audit catches
mismatches.

## Slug discipline

- Lowercase only. Hyphens, no underscores. No file extensions in slugs.
- Dates in slugs use `YYYY-MM-DD` (sortable).
- X handles in `corpus/` and `media/social/` use `x-<handle>` (lowercase
  handle, no `@`). Dated social pages append `-YYYYMMDD`.
- Meetings use `YYYY-MM-DD-<participant-or-context>-<topic>`. Multiple
  participants are joined with hyphens (e.g. `alice-bob`).

## Back-linking iron law (non-negotiable)

Every page that **mentions** a person / company / concept / project that
already has a brain page MUST add a bidirectional link:

1. The mentioning page wikilinks the entity: `[[people/garry-tan]]`.
2. The entity's page gets a timeline entry on the same date:
   ```markdown
   - **2026-05-08** | Mentioned in [[<source-page>]] — <one-line context>
   ```

This is the iron law. Without back-links, retrieval falls apart — half
the value of the brain is the link graph, not the page bodies. The
audit `gbrain dream extract` rebuilds links from page text, but it only
works if the wikilink is present in the source page to begin with.

## Common misfiling patterns — DO NOT DO

| Wrong | Right | Why |
|---|---|---|
| Article *about* Garry → `sources/` only | Also link from `people/garry-tan` | The page is about Garry; people pages are the entry point |
| Meeting notes → `meetings/` only | Also enrich every entity mentioned | Meetings without back-links rot |
| Concept stub at top-level (e.g. `ai-agents.md`) | Move to `concepts/ai-agents.md` | Top-level pages skip the routing table; future skills won't find them |
| Twitter thread analysis → `media/` only | If it teaches a reusable mental model, also create `concepts/<model-name>` and link both ways | Primary subject rule — the model is the durable artefact |
| New entity dir (`vc-firms/`, `frameworks/`) | Use existing `companies/` or `concepts/`, OR update this file FIRST | Dir sprawl breaks the routing table |
| Mid-stream lower-case-with-spaces slug (e.g. `personal ai assistant`) | `personal-ai-assistant` | Slug discipline — hyphens, no spaces |

## Currently flagged anti-patterns in {{user.brain_repo}} (2026-05-08)

These exist today and should be cleaned up — captured here so future
audits can confirm they shrink to zero.

- 7 loose top-level concept stubs that should live in `concepts/`:
  `ai-agents.md`, `anthropic-claude.md`, `claude.md`, `codex.md`,
  `founder-mode.md`, `personal-brain.md`, `prompt-engineering.md`. All
  are `type: concept` per their frontmatter — the dir mismatch is the
  bug. Cleanup is queued, not gated.
- Possible duplication: `concepts/ai-agent.md` vs the loose
  `ai-agents.md`. Dedupe in next `gbrain dream` synthesise pass.

## How skills consult this file

In SKILL.md frontmatter:

```yaml
writes_to:
  - habits/water-intake          # canonical brain dir from the table above
brain_dirs_read:
  - habits/water-intake
```

Anything in `writes_to` that doesn't appear in the canonical table is
either (a) a typo, (b) a misfile, or (c) a sign the table needs an
entry — but only after a real-world need is shown, not pre-emptively.

## Update cadence

- Edit this file the **first** time a skill needs a new dir, and
  document the rule before adding any pages.
- Sunday hygiene: scan for new top-level dirs in the brain that don't
  appear in the canonical table; either delete the dir or add the rule.
- Monthly: re-run the loose-page anti-pattern scan. Goal: zero loose
  top-level pages outside the curated meta files (`README.md`,
  `_inbox/`).
