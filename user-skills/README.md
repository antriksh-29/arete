# user-skills/

This directory holds skills that **Alfred wrote himself** (or that Alfred
forked from `skills/` / `openclaw/skills/`) in response to a Telegram
conversation. The flow that writes here is `meta/skill-author/SKILL.md`.

## Precedence

`user-skills/<name>/` wins on every name conflict against `skills/<name>/`
and `openclaw/skills/<name>/`. See `identity/AGENTS.md` → `## User-skills
overlay` for the full rule and `infra/deploy-vps.sh:sync_user_skills()`
for the deploy-side enforcement.

## What lives here

Each subdirectory is one user-skill:

```
user-skills/<slug>/
├── SKILL.md          # frontmatter + body, same shape as skills/<name>/SKILL.md
├── eval.yaml         # routing fixtures
└── .usage.json       # {use_count, last_used_at, error_count, last_error}
                     #   written by infra/usage-tracker.py from Axiom logs
```

**v1 file allowlist:** only the three files above. No `scripts/`, no
`references/`, no `.test.ts`, no `.py`. SkillJect HIDEPAYLOAD attack
motivates this — see the threat-model note in `meta/skill-author/SKILL.md`.
If a skill needs custom logic, author it as a regular `skills/<name>/`
skill via Claude Code instead.

## Frontmatter conventions

Skills in this directory carry extra frontmatter fields the rest of the
repo doesn't use:

- `created_by: alfred` — Hermes-style provenance, distinguishes from
  hand-authored `skills/`
- `source: telegram` — how this skill was authored
- `forked_from: <tier>/<name>@<commit-sha>` — present only when the
  skill was created via `/refine` against an existing `skills/` or
  `openclaw/skills/` skill (fork-on-modify, see U1 in the plan)
- `version: 0.1.0` — bumps on every `/refine` turn (semver-ish; PATCH
  per refine, MINOR when triggers change)

## Hand-editing is fine

You can edit any file here in Claude Code if you want. The `.usage.json`
sidecar will be overwritten by `infra/usage-tracker.py` on the next
resolver hit; the SKILL.md and eval.yaml will survive. If you want
Alfred to forget a skill entirely, just `rm -rf user-skills/<slug>/`
and the next deploy's zombie-deletion pass removes it from the
workspace too.

## Don't write secrets here

`user-skills/` is committed to git just like `skills/`. Vault references
(`bot-cred get FOO`) are fine. Inlined credentials are not. The safety
scanner in `meta/skill-author/scripts/safety-scan.ts` rejects commits
that look like inlined secrets, but if you hand-edit, you bypass it.
