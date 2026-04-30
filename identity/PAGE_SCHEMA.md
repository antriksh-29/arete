# Brain page schema

How brain pages (people, companies, projects, concepts, meetings, sources) are
structured. This is the compiled-truth-above + timeline-below pattern from
Garry Tan's gbrain design — required for hybrid retrieval (vector + keyword +
RRF) to rank well.

The schema is enforced by `gbrain` skills (`enrich`, `ingest`, `idea-ingest`,
`citation-fixer`). Don't hand-edit pages in ways that break the structure.

---

## Why two halves

A page has two regions, separated by `## Recent activity` (or equivalent
timeline header):

- **Above the timeline = compiled truth.** Stable, deduplicated, source-cited
  statements. The agent reads this first when answering "what do I know about
  X". This is what gets vector-embedded for retrieval.
- **Below the timeline = raw timeline.** Append-only log of interactions,
  observations, ingestions. Each entry dated and source-cited. This is the
  audit trail; it's where new information lands before being compiled up.

Compiled truth is what the agent surfaces. The timeline is what the agent
trusts. The `enrich` skill periodically promotes timeline entries into
compiled truth (with source citations) and demotes stale claims.

If you flatten this — putting everything in one chronological log, or putting
everything in one compiled summary — retrieval degrades and the agent can't
distinguish "what I know now" from "what happened on date X".

---

## Frontmatter (required)

```yaml
---
type: person | company | project | concept | meeting | source
name: Display Name
aliases: [Other Name, Nickname]
created: 2026-04-30
updated: 2026-04-30
tags: [topic-1, topic-2]
---
```

The slug is path-derived: `<type-folder>/<filename-without-md>` (e.g.
`people/jane-doe`). Do not set `slug:` in frontmatter — gbrain rejects pages
where the frontmatter slug doesn't match the path. Filename is the canonical
identifier used in entity links (`[[person:jane-doe]]`). `aliases` feed the
keyword-search half of hybrid retrieval — list every name, nickname, handle,
email-local-part you'd plausibly type.

---

## Body structure

```markdown
# Display Name

## Summary
One paragraph compiled truth. Who/what this is, why it matters to me.
[Source: <where this came from>, <date>]

## Key facts
- Fact one. [Source: ...]
- Fact two. [Source: ...]

## Recurring topics       (people/companies)
## Open threads           (people/projects)
## Status                 (projects)
## Definition             (concepts)

---  (timeline divider; the `## Recent activity` header below is the boundary)

## Recent activity
- 2026-04-30 — what happened, observed, or was ingested. [Source: ...]
- 2026-04-29 — earlier entry. [Source: ...]
```

Every claim above the timeline cites a source. Every timeline entry cites a
source. `citation-fixer` runs periodically and flags uncited claims.

---

## Entity links

Cross-page references use `[[type:slug]]` syntax:

- `[[person:jane-doe]]`
- `[[company:acme]]`
- `[[project:keystone-bet]]`

These are typed and bidirectional. `gbrain` resolves them into a knowledge
graph used by `query` and `briefing` skills. Don't use plain markdown links
for in-brain references.

---

## Sources

Citation format: `[Source: <origin>, <date>]`. Origins:

- `User, conversation, 2026-04-30`
- `Gmail, msg-id-abc123, 2026-04-30`
- `Calendar, event-id-xyz, 2026-04-30`
- `User, brain seed, 2026-04-29` (hand-written seed pages)

Origin should be specific enough that the agent could re-fetch or verify if
needed. Vague sources ("internet", "I think") fail `citation-fixer`.

---

## Page types — quick reference

| Type     | Required sections beyond Summary/Key facts                |
|----------|-----------------------------------------------------------|
| person   | Recurring topics, Open threads, Recent activity           |
| company  | Recurring topics, Recent activity                         |
| project  | Status, Open threads, Recent activity                     |
| concept  | Definition, Recent activity                               |
| meeting  | Attendees, Decisions, Action items, (no timeline — leaf)  |
| source   | What it is, Why I trust it, Recent activity               |

Meetings are leaf pages: they don't have a timeline because the meeting
itself is a single timeline entry on the relevant person/project/company
pages.

---

## When to write by hand vs let the agent populate

- **Agent-populated (default):** ingestion skills create stub pages and
  append timeline entries. `enrich` compiles them up. This is the design;
  prefer it.
- **Hand-written seeds (rare):** for entities you reference often that the
  agent hasn't seen yet, drop a 5-10 line seed page. Frontmatter +
  Summary + 2-3 Key facts is enough. The agent fills the rest from
  conversation. Mark seed lines `[Source: User, brain seed, <date>]`.

Don't hand-maintain pages long-term. The agent is the writer; you're the
reader and (occasionally) the corrector.
