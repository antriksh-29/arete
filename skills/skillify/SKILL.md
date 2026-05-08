---
name: skillify
version: 2
script: infra/skillify.py
mutating: false
cron: null
eval: skills/skillify/eval.yaml
last_audited: 2026-05-08
description: |
  The meta skill. Turn any raw feature or script into a properly-skilled,
  tested, resolvable, evaled unit of agent-visible capability. Two roles
  in one skill: (a) the methodology — what it means for a feature to be
  "properly skilled" — and (b) the runtime audit — `skillify audit
  <name>` reports per-stage which artefacts exist for a given skill.
  WHEN: User says "skillify this", "is this a skill?", "make this
  proper", "audit <skill>", "is <skill> skillify-clean", "what's
  missing for <skill>", or asks for the discipline. Also fires
  implicitly when the agent recognises a recurring pattern in
  conversation that should graduate from one-off to skill.
  NOT WHEN: The user wants to edit an existing skill (just edit the
  SKILL.md directly). NOT for catalog-wide drift (→ skill-drift).
trigger: |
  - "skillify this" / "skillify"
  - "is this a skill?" / "make this proper"
  - "make this a skill" / "codify this" / "let's turn this into a skill"
  - "add tests and evals for this"
  - "check skill completeness"
  - "what's the skillify discipline" / "what are the stages"
  - "audit <skill> against the cycle" / "is <skill> skillify-clean"
  - "what's missing for <skill>"
writes_to: []
brain_dirs_read: []
---

# Skillify — The Meta Skill

## Contract

A feature is "properly skilled" when all ten checklist items are present:

1. `SKILL.md` — skill file with YAML frontmatter, triggers, contract, phases.
2. Code — deterministic script if applicable.
3. Unit tests — cover every branch of deterministic logic.
4. Integration tests — exercise live endpoints, not just in-memory shape.
5. LLM evals — quality/correctness cases if the feature includes any LLM call.
6. Resolver trigger — `skills/RESOLVER.md` entry with the trigger patterns
   the user actually types.
7. Resolver trigger eval — test that feeds trigger phrases to the resolver
   and asserts they route to this skill, not the old pre-skillify path.
8. Check-resolvable — `gbrain check-resolvable` passes (skill is reachable,
   MECE against its siblings, no DRY violations).
9. E2E test — exercises the full pipeline from user turn to side effect.
10. Brain filing — if the feature writes brain pages, `brain/RESOLVER.md`
    has an entry for the directory so the pages aren't orphaned.

## Phases

### Phase 1: Audit what exists

For the feature being skillified, answer:

- **Feature name**: what does it do in one line?
- **Code path**: where does the implementation live (file path)?
- **Checklist status**: run our local audit (`python3 ~/bin/skillify.py
  audit <name>`) for a per-stage gap report against this repo's
  conventions, OR `gbrain skillify check <path>` for the gbrain-native
  10-item scorecard. Note which items are missing.

### Phase 2: Create missing pieces in order

**Fast path — brand-new skill:** run `gbrain skillify scaffold <name>
--description "..." [--triggers "p1,p2,p3"] [--writes-pages --writes-to
"people/,companies/"]`. This creates all 5 stub files atomically and
appends an idempotent resolver row. Every scaffolded file carries the
`SKILLIFY_STUB` sentinel; `gbrain check-resolvable --strict` will fail
CI until you replace the stubs with real content.

**Manual path — extending an existing skill:** work the list top-down.
Each earlier item constrains what later items look like (the SKILL.md
contract determines what tests assert; tests determine what evals gate;
the resolver entry determines what trigger-eval checks).

1. Write `SKILL.md` first. Frontmatter must include `name`, `version`,
   `description`, `trigger` (multi-line) or `triggers[]`, `mutating`,
   `script`, `eval`, `cron`, `last_audited`, `writes_to`,
   `brain_dirs_read`. Body has at minimum Contract, Phases, and Output
   Format sections. See `eval/schemas/skill.schema.md` for the full
   field reference.
2. Extract deterministic code into a script if applicable (scripts/*.ts
   for gbrain; host projects may use .mjs / .py / whatever their runtime
   uses). Each backing script must support `--smoke` returning an
   `{"ok":true,...}` envelope with no network or side effects.
3. Write unit tests for every branch of the script. Mock external calls
   (LLM, DB, network) so tests run fast and deterministic.
4. Add integration tests that hit real endpoints. These catch bugs the
   unit tests' mocks hide (see the `files-test-reimplements-production`
   learning: reimplementation in tests lets production vulnerabilities
   slip through).
5. Add LLM evals if the feature includes any LLM call. Even a three-case
   eval (happy / edge / adversarial) is cheap insurance against prompt
   regressions. The host repo's `eval.yaml` `judge_rubric` field +
   `--judge` flag wires this through gpt-5.4-nano automatically.
6. Add the resolver trigger to `skills/RESOLVER.md` (or in the host
   repo, a SKILL.md `trigger:` block). Use the trigger patterns the
   user ACTUALLY types, not what you think they should type.
7. Add a resolver trigger eval that feeds those patterns in and asserts
   they route to the new skill. Negative cases (similar phrases that
   should NOT route here) are critical — that's where false positives
   live.
8. Run `gbrain check-resolvable` (auto-detects skill trees) or
   `gbrain check-resolvable --skills-dir <path>` for custom locations.
   The host repo also ships `node infra/check-resolvable.mjs` for
   bogota-side audit (validates SKILL.md frontmatter shape, script
   existence, eval pointer, brain-dir validity).
9. Add an E2E smoke test. For gbrain: submit a Minion job or run a CLI
   invocation end-to-end against a fixture brain; assert side effects.
   For host repos: a drop-in `infra/smoke-tests/50-skill-<name>.sh`
   wrapper that runs the script's `--smoke` flag.
10. Update `brain/RESOLVER.md` (or the host's `BRAIN_FILING.md`) if the
    skill writes brain pages. Orphaned brain pages are worse than no
    brain pages.

### Phase 3: Verify

Run each of these and confirm green:

```bash
# Unit tests
bun test test/<skill-name>.test.ts

# Integration tests (when applicable)
bun run test:e2e

# Resolver reachability + MECE + DRY
gbrain check-resolvable

# Conformance tests (skill YAML + required sections)
bun test test/skills-conformance.test.ts

# In a host repo using the bogota patterns:
bash eval/run-all-evals.sh             # cheap pre-push gate
bash eval/run-all-evals.sh --judge     # deeper LLM-judge run
```

## Quality gates

A feature is NOT properly skilled until:

- All tests pass (unit + integration + evals).
- It appears in `skills/RESOLVER.md` (or host's resolver) with accurate
  trigger patterns.
- The resolver trigger eval confirms patterns route to the new skill.
- `gbrain check-resolvable` shows no orphaned skills, no MECE overlaps,
  no DRY violations.
- If it writes brain pages, `brain/RESOLVER.md` (or host
  `BRAIN_FILING.md`) has the directory.

## Anti-Patterns

- ❌ Code with no SKILL.md — invisible to the resolver; the agent will
  never run it.
- ❌ SKILL.md with no tests — untested contract; one prompt change
  regresses silently.
- ❌ Tests that reimplement production code — the reimplementation's
  bugs don't catch production's bugs (the `files-test-reimplements-
  production` lesson).
- ❌ Resolver entry that uses internal jargon the user never types —
  trigger patterns must mirror real user language.
- ❌ Feature that writes to brain without a `brain/RESOLVER.md` entry —
  orphaned pages the agent will never find.
- ❌ Deterministic logic in LLM space — should be a script.
- ❌ LLM judgment in deterministic space — should be an eval.

## Why skillify + check-resolvable is the right pair

Hermes and similar agent frameworks auto-create skills as a background
behavior. That's fine until you don't know what the agent shipped —
checklists decay, tests drift, resolver entries get stale.

Gbrain ships the same capability as two user-controlled tools:

- `/skillify` builds the checklist and helps you fill in the gaps.
- `gbrain check-resolvable` validates the whole skill tree: reachability,
  MECE, DRY, gap detection, orphaned skills.

You decide when and what. The human keeps judgment. The tooling keeps the
checklist honest. In practice this combo produces zero orphaned skills,
every feature with tests + evals + resolver triggers + evals of the
triggers.

## Runtime tooling (host-repo addition)

This skill ships with a runtime audit at `infra/skillify.py`. Three
modes for use inside a conversation:

```bash
~/bin/skillify.py checklist           # print the discipline (markdown)
~/bin/skillify.py audit <skill-name>  # per-stage gap report (JSON)
~/bin/skillify.py --smoke
```

The `audit` subcommand maps the 10-item contract above to concrete
artefact checks against the host repo:

| Stage | Audit signal |
|---|---|
| Concept | `skills/<name>/SKILL.md` exists |
| Prototype | implied if SKILL.md exists |
| Evaluate | `eval.yaml` exists with ≥ 5 triggers |
| Codify | the `script:` field points to a real file |
| Cron | `cron:` is `null` (on-demand) or a real cron string |
| Monitor | `infra/smoke-tests/50-skill-<name>.sh` wrapper present |

`gaps[]` lists the failures. `ok: true` only when every stage is clean.
Surface gaps as concrete next-action items, not abstract complaints.

## Output Format

A skillify run produces, in order:

1. An audit printout listing which of the 10 items exist and which are
   missing for the target feature.
2. The files created to close each gap (SKILL.md, test files, resolver
   entries).
3. The final `gbrain check-resolvable` output confirming reachability.

> **If the user asks for the same thing twice and it's not yet a skill,
> you failed step 1.**
