---
name: humanizer
version: 1
script: infra/humanizer.py
mutating: false
cron: null
eval: skills/humanizer/eval.yaml
last_audited: 2026-05-08
description: |
  Deterministic AI-tell stripper. Removes em-dashes, AI vocabulary
  (delve / tapestry / pivotal / navigate the landscape / furthermore),
  inflated symbolism (symphony of, dance of, treasure trove of), vague
  attributions ("studies show", "some say", "widely regarded"), and
  sycophantic openers ("Great question!", "Certainly,", "Absolutely!").
  Pure regex; no LLM, no network. Complements identity/SOUL.md voice
  tuning — SOUL.md says HOW to write; humanizer enforces what NOT to
  slip in.
  WHEN: User asks to "humanize" / "de-AI" / "strip AI-isms" from a
  block of text, or post-processes outbound writing before sending.
  Also wired as a constraint pass on agent-composed long replies (Wave
  4B governance).
  NOT WHEN: User asks for a stylistic rewrite of a different kind
  (more concise, more formal, in <person>'s voice — that's x-draft or
  free-form rewrite, not humanizer).
trigger: |
  - "humanize this"
  - "de-AI this" / "remove AI-isms" / "strip the AI tells"
  - "rewrite without AI tells"
  - "make this sound less like an AI" / "less LLM-y"
  - "remove the em-dashes" / "strip 'delve' and 'tapestry'"
writes_to: []
brain_dirs_read: []
---

# /humanizer

Deterministic regex pass over a block of text. Cheap (microseconds),
idempotent. Lives at `infra/humanizer.py`; OpenClaw also installs the
community version (`openclaw skill install humanizer`) — our local
script is the canonical entry, the OpenClaw skill is a fallback.

## How to invoke

```bash
echo "<text>" | python3 ~/bin/humanizer.py        # JSON envelope
python3 ~/bin/humanizer.py --diff "<text>"        # human-readable side-by-side
python3 ~/bin/humanizer.py --smoke
```

## What it strips

| Category | Examples |
|---|---|
| Em-dashes | `—` between words → comma; line-leading `—` → removed |
| AI vocabulary | "delve into", "rich tapestry of", "navigate the landscape", "furthermore", "moreover", "in essence", "it's worth noting that", "pivotal", "meticulous", "seamless" |
| Inflated symbolism | "a symphony of", "a dance of", "a wealth of", "a treasure trove of" |
| Vague attributions | "some say that", "studies show that", "it could be argued that", "widely regarded as" |
| Sycophantic openers | "Great question!", "Certainly,", "Absolutely,", "Happy to help" |

## Result envelope (stdout JSON)

```json
{
  "ok": true,
  "input": "<original>",
  "output": "<rewritten>",
  "edits": [{"rule": "em_dash_inline", "matches": 2}, ...]
}
```

## Telegram reply

When the user explicitly invokes ("humanize this"), relay the
`output` field. When humanizer runs as a constraint pass on the
agent's own composed reply, the output replaces the candidate reply
silently before send.

## Architectural rules

- **Idempotent.** Running humanizer twice on the same text produces
  the same output. The eval verifies this.
- **No model calls.** Pure regex. The whole point is determinism.
- **Mangled output is a signal**, not a bug — if the rewritten text
  reads incoherently, the upstream model used too many AI-tells.
  Tune the upstream prompt, not the humanizer.
- **Don't humanize user input.** The skill targets outbound bot text
  only. User text is sacred (per the verbatim-rule in AGENTS.md).

## Smoke

`humanizer.py --smoke` returns `{"ok": true, ...}` with no input.
