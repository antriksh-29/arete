---
name: identity-audit
version: 1
script: infra/identity-audit.py
mutating: false
cron: "0 18 * * 0"   # Sunday 18:00 IST — surfaces in weekly retrospective
eval: skills/identity-audit/eval.yaml
last_audited: 2026-05-08
description: |
  Lints identity/*.md (AGENTS.md, USER.md, SOUL.md, HEARTBEAT.md, etc.)
  for transient content that probably doesn't belong in a constitutional
  document. Per Yegge / Tan thinking — Layer-1 working memory should
  be slow-changing constitution, not "this week's project" snapshots
  that rot. The audit surfaces candidates; never edits.
  WHEN: User asks "audit identity files" / "anything stale in AGENTS.md"
  / "what's transient in my constitution" / "identity audit". Also
  fires Sunday 18:00 IST as part of the weekly retrospective.
  NOT WHEN: User wants to audit a specific skill against the skillify
  cycle (→ skillify audit). NOT for live state queries (→ brain).
trigger: |
  - "identity audit" / "audit identity files"
  - "anything stale in AGENTS.md"
  - "what's transient in my constitution"
  - "lint identity"
  - "is my identity drifting"
writes_to: []
brain_dirs_read: []
---

# /identity-audit

Backed by `infra/identity-audit.py`. Categorizes findings as:

| Category | Pattern | Why |
|---|---|---|
| `date` | `YYYY-MM-DD` references | Specific dates rot; usually deadlines or as-of markers |
| `state` | "currently", "right now", "in flight", "in progress" | In-flight state belongs in a brain query, not a doc |
| `recency` | "this week", "today's", "yesterday's", "recent", "latest" | Recency framing scopes content to 'now' |
| `status` | TODO / FIXME / WIP / XXX markers | Should live in a tracker |
| `deadline` | "soak ends", "expires", "deadline", "due by/on" | Auto-expires; better tracked elsewhere |

For `date` hits, the audit also reports the offset in days: `9d offset`,
`-18d offset` (future), `58d old` (past). Anything > 30 days old is
extra-suspect.

## How to invoke

```bash
~/bin/identity-audit.py                 # JSON, all identity/*.md
~/bin/identity-audit.py --markdown      # human-readable
~/bin/identity-audit.py --file <path>   # single file
~/bin/identity-audit.py --smoke
```

## What it does NOT do

- **No automatic edits.** Constitutional files only change with user
  judgment. The script flags candidates; the user decides keep / move /
  delete.
- **No "fix-it" mode.** Even with a "yes delete that line" approval,
  that's a separate Edit tool call by the user, not by this skill.
- **No correctness claim.** It produces *candidates*, not *errors*.
  False positives are fine — the goal is to make transient content
  visible.

## Triage guidance

Each finding is one of three things:

1. **Constitutional** (keep) — slow-changing fact, rule, or principle.
   Date references that are intentionally durable (e.g., "soak ends
   2026-05-26" tracking a planned safety milestone) are fine until
   they pass; flag the deadline in your calendar to revisit.
2. **Belongs elsewhere** (move) — transient state that should be a
   brain query (`gbrain query "what am I working on"`), a tracker
   entry, or a brain page (e.g., per-project pages in `projects/`).
3. **Stale** (delete) — content that's no longer true and isn't worth
   moving anywhere.

## Resolver patterns

| User intent | Command |
|---|---|
| "identity audit" / "audit identity" | `~/bin/identity-audit.py --markdown` |
| "audit AGENTS.md" / "lint AGENTS.md" | `~/bin/identity-audit.py --file identity/AGENTS.md --markdown` |
| "stale dates in identity" | filter the JSON output to `category=date` |

## Telegram reply

For ad-hoc invocation, relay the `user_message` summary + a top-3
findings preview. Don't paste the full markdown table — too long for
chat. Point the user at the report path or offer to dump the markdown
into `.context/identity-audit-<date>.md`.

## Architectural rules

- **The audit is a lint, not a gate.** Findings don't block anything.
  The pre-push gate is the gate.
- **Sunday cadence** — runs as part of the weekly retrospective
  alongside the skill-drift report. Both surface "what rotted last
  week" rather than "what just broke."

## Smoke

`identity-audit.py --smoke` returns a static envelope.
