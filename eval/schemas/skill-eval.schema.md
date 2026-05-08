# skills/&lt;name&gt;/eval.yaml schema (v2)

Every skill ships its own eval suite. The harness
`eval/run-skill-eval.mjs` runs four kinds of check, layered cheap → costly:

1. **Smoke** — script callable with `--smoke` flag (no network, no LLM).
2. **Output structural** — for each output entry: run `cmd`, parse stdout
   as JSON, assert `expect_json_keys` + `expect_ok` (if specified).
3. **Trigger routing** *(needs `OPENAI_API_KEY`)* — gpt-4o-mini scores
   each utterance against the resolver table. Pass: ≥ 90% top-1.
4. **Output judge** *(opt-in via `--judge`, needs `OPENAI_API_KEY`)* —
   gpt-5.4-nano scores each rubric-tagged output's actual stdout against
   its `judge_rubric:`. Pass: ≥ 8/10 per entry.

## Pass thresholds

| Check | Pass bar |
|---|---|
| Smoke | exit 0 |
| Output shape | 100% of `expect_json_keys` present, `expect_ok` matches |
| Trigger routing | ≥ 90% top-1 accuracy |
| Output judge | ≥ 8/10 per entry |

A skill's eval is **not green** unless all enabled checks pass. Skipped
checks (no API key, `--judge` not set) don't fail the verdict — they're
just not enforced for that run.

## Cost model

| Mode | Per skill (avg) | Per `run-all-evals.sh` |
|---|---|---|
| `--no-llm` (pre-push default) | 0¢ | 0¢ |
| default | ~20–30 LLM calls (gpt-4o-mini for trigger routing) | ~$0.01 |
| `--judge` | trigger routing + 2–4 judge calls per skill | < $0.05 total |

`gpt-5.4-nano` is the cheapest tier. Fine for binary rubric scoring.

## Schema

```yaml
skill: <name>                   # must match SKILL.md name
script: infra/<file>.py         # mirror of SKILL.md script field
version: 1                      # eval suite version, independent of SKILL.md version

triggers:                       # ≥ 20 entries (mix of positive + negative)
  - id: <slug>-001
    utterance: "<what the user types>"
    has_image: false            # optional; default false
    has_url: false              # optional; default false
    expect_skill: <name>        # the expected resolver pick
    notes: "<why this case matters>"

outputs:                        # ≥ 3 entries covering happy + edge + error paths
  - id: <slug>-out-001
    fixture: eval/fixtures/<skill>/<file>     # optional input file
    cmd: "python3 infra/<file>.py {fixture}"  # {fixture} is substituted
    expect_json_keys: [ok, user_message]      # keys that must be present
    expect_ok: true                           # value of `ok` field
    judge_rubric: |
      Output must <one paragraph criterion>.
      Hard fails: <list of must-not patterns>.

smoke:
  cmd: "python3 infra/<file>.py --smoke"
  expect_exit: 0
  notes: "Cold-boot callability check. Should not require network."
```

## Adding a new eval entry after a real failure

When the bot fails at runtime in a way the eval didn't catch, the
**first thing** that happens is a new entry gets added to this file
(see Blog 5: "every 'the bot got that wrong' → new entry"). The eval
grows; AGENTS.md does not.

## Fixtures

Anything referenced via `{fixture}` lives at `eval/fixtures/<skill>/`.
Keep them small (≤ 100KB) and check them into git. For inputs that
can't be checked in (X API responses, gated PDFs), commit a sanitized
JSON capture and have the script read from `--fixture-mode <path>`.
