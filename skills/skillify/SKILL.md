---
name: skillify
version: 1
script: infra/skillify.py
mutating: false
cron: null
eval: skills/skillify/eval.yaml
last_audited: 2026-05-08
description: |
  Codifies the manual→prototype→evaluate→codify→cron→monitor cycle so
  a recurring workflow becomes a permanent skill instead of a
  one-off task. Two on-demand uses: print the discipline, audit a
  specific skill against it. Also a meta-skill the agent itself
  invokes when it notices "the user is asking for X for the second
  time and X is not yet a skill."
  WHEN: User says "skillify this" / "make this a skill" / "codify this
  workflow" / asks for the discipline / asks how a specific skill
  measures up. Also fires implicitly when the agent recognises a
  recurring pattern in conversation that should graduate.
  NOT WHEN: The thing is genuinely one-off. NOT for editing an
  existing skill (just edit the SKILL.md directly).
trigger: |
  - "skillify this" / "make this a skill" / "codify this"
  - "let's turn this into a skill"
  - "what's the skillify discipline" / "what are the stages"
  - "audit <skill> against the cycle" / "is <skill> skillify-clean"
  - "what's missing for <skill>"
writes_to: []
brain_dirs_read: []
---

# /skillify

Backed by `infra/skillify.py`. Three modes:

```bash
~/bin/skillify.py --smoke
~/bin/skillify.py checklist
~/bin/skillify.py audit <skill-name>
```

## The discipline (the canonical 6 stages)

1. **Concept** — describe in plain language. What does the user say? What output do they need?
2. **Prototype** — manually on 3–10 real items. NO `SKILL.md` yet.
3. **Evaluate** — show the user, iterate on tone/format/scope, stop when they say it's right.
4. **Codify** — write `skills/<name>/SKILL.md` per `eval/schemas/skill.schema.md` + `eval.yaml` with ≥ 20 trigger entries.
5. **Cron** — schedule via gbrain autopilot or systemd timer if recurring; `cron: null` if on-demand.
6. **Monitor** — every "the bot got that wrong" → new eval entry. Skill-drift weekly report (G8) catches silent decay.

> *If the user asks for the same thing twice and it's not yet a skill, you failed step 1.*

## Audit output

`audit <skill>` returns a JSON envelope with per-stage findings:

| Stage | Audit signal |
|---|---|
| Concept | `skills/<name>/SKILL.md` exists |
| Prototype | implied if SKILL.md exists (process can't be audited post-hoc) |
| Evaluate | `eval.yaml` exists + has ≥ 5 triggers |
| Codify | the `script:` field in SKILL.md points to a file that exists |
| Cron | `cron:` field is either `null` (on-demand) or a real cron string |
| Monitor | `infra/smoke-tests/50-skill-<name>.sh` wrapper present |

`gaps[]` lists the failures. `ok: true` only when every stage is clean.

## Resolver patterns

| User intent | Command |
|---|---|
| "skillify this" / "what's the discipline" | `python3 ~/bin/skillify.py checklist` |
| "audit <skill>" / "is <skill> clean" | `python3 ~/bin/skillify.py audit <skill>` |
| "what's missing for <skill>" | `python3 ~/bin/skillify.py audit <skill>` and surface `gaps[]` |

## Telegram reply

For `checklist` — relay verbatim (already markdown-formatted).
For `audit` — extract `user_message` from the JSON. If the audit shows
gaps, list them as concrete next actions, not abstract complaints.

## Architectural rules

- **Skillify discipline is the rule that makes the system compounding.**
  A bot that doesn't graduate recurring tasks into skills is a bot
  that re-discovers the same shape every conversation.
- **Audit is a lint, not a gate.** Failing skillify-audit doesn't block
  anything — it surfaces a debt to address. The pre-push gate is the
  gate.
- **Don't auto-skillify.** The agent recognising "this is recurring"
  must always confirm with the user before writing files. The
  prototype stage is non-negotiable; we don't ship a SKILL.md for an
  unproven workflow.

## Smoke

`skillify.py --smoke` returns a static envelope.
