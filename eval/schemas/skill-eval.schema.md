# skills/&lt;name&gt;/eval.yaml schema (v1)

Every skill ships its own eval suite. The harness
`eval/run-skill-eval.mjs` reads this file and runs four kinds of check:

1. **Trigger routing** — for each utterance, does the resolver pick this skill?
2. **Output shape** — given a fixture input, does the script produce the
   expected JSON envelope?
3. **Smoke** — is the script callable end-to-end with a no-op flag?
4. **Judge rubric** — LLM-as-judge scores output against a written rubric.

## Pass thresholds (Wave 1 baseline)

| Check | Pass bar |
|---|---|
| Trigger routing | ≥ 90% top-1 accuracy |
| Output shape | 100% of `expect_json_keys` present, `expect_ok` matches |
| Smoke | exit 0 |
| Judge rubric | ≥ 8/10 average |

A skill's eval is **not green** unless all four pass.

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
