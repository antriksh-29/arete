# SKILL.md frontmatter schema (v1)

Every `skills/<name>/SKILL.md` MUST start with a YAML frontmatter block
matching this shape. The audit (`infra/check-resolvable.mjs`) parses these
fields directly — if they're missing or malformed, the audit fails.

## Required fields

| Field | Type | Notes |
|---|---|---|
| `name` | string | Skill slug. Must equal the directory name. |
| `version` | int | Bump on contract change (rename, removed trigger, output shape change). |
| `script` | string | Repo-relative path to the deterministic script. Must exist. |
| `mutating` | bool | True if the skill writes to brain, disk, or external service. |
| `cron` | null \| string | Cron expression if scheduled, else `null`. |
| `eval` | string | Repo-relative path to the eval YAML. Must exist (file may be skeleton). |
| `last_audited` | YYYY-MM-DD | Set by `check-resolvable.mjs` on each run. Hand-set on creation. |
| `description` | string (multi-line) | Resolver-facing. See **Description discipline** below. |
| `trigger` | string (multi-line) | Bullet list of utterance patterns. Used by `dry-audit.mjs`. |
| `writes_to` | list[string] | Brain dirs / files written. `[]` if read-only. |
| `brain_dirs_read` | list[string] | Brain dirs queried for context. `[]` if doesn't read brain. |

## Optional fields

| Field | Type | Notes |
|---|---|---|
| `helper_scripts` | list[string] | Secondary scripts the main one calls. |
| `requires_creds` | list[string] | Env-var or vault names. Audited at boot. |
| `model_tier` | string | `vision`, `cheap`, `default`, `deep` — see `identity/MODEL_ROUTING.md`. |
| `mutates_external_state` | bool | True if the skill mutates non-brain state (systemd, files outside brain, etc.). Required when `mutating: true` and `writes_to: []`. |
| `trigger_guard` | string | Modality guard. One of: `image_attached`, `pdf_url`, `x_url`, `any_url`. When two skills legitimately share a trigger phrase but disambiguate by input modality (e.g. "read this" with image vs PDF URL), declare distinct guards so DRY audit treats the overlap as WARN not FAIL. Skills without a guard fire on text-only intent. |
| `constraint_skill` | bool | True for skills that filter every outbound message rather than firing on user intent (e.g. `reply-discipline`). Audit skips trigger-routing checks for these. |

## Description discipline

The `description` field is what the resolver matches user intent against.
Lead with the user goal, follow with `WHEN:` and `NOT WHEN:` blocks that
disambiguate against other skills. Bad: *"Ingests PDFs into the brain."*
Good:

```
description: |
  PDF ingestion. The user shares a PDF and wants it absorbed into the
  brain as a durable analyzed source page.
  WHEN: PDF URL or attachment + "ingest", "absorb", "remember this".
  NOT WHEN: User wants it queued for later (→ reading-list), or it's
  an image not a PDF (→ image-ocr).
```

The `NOT WHEN` clause is what prevents resolver false-positives. Every
new skill must list at least one negative case.

## Trigger discipline

Triggers are matched verbatim (lowercased, stripped) by the DRY audit.
A trigger phrase claimed by ≥2 skills is a hard fail — pick a more
specific phrase or add a NOT WHEN clarifier.

## Body sections (recommended, not enforced)

A consistent body shape helps the agent at runtime find the same
information in the same place across all skills.

```
## Intent
## How to invoke
## Result envelope
## Telegram reply format
## Resolver patterns
## Architectural rules
```

Body content is not parsed by the audits — only the frontmatter is.
