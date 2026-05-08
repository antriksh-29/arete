# eval/

Evaluation artifacts for the personal AI assistant. Five distinct suites
covering five different failure modes. None replace the others.

| Suite | File | Question | Run when |
|-------|------|----------|----------|
| **Injection fixture** | `../openclaw/_smoke/injection-fixture.yaml` | Does the bot **refuse** malicious instructions? | Every systemd start (boot blocks on miss) |
| **Retrieval eval** | `retrieval-eval.yaml` | Does the brain return the **right pages** for a query? | Weekly + algorithm changes |
| **Behavioral eval** | `behavioral-eval.yaml` | Does the bot **do good things well** — accurate answers, correct skill routing, useful synthesis? | Weekly + skill changes |
| **Skill evals** | `../skills/<name>/eval.yaml` | Does *this specific skill* route correctly + produce a valid output envelope? | Pre-push + on skill change |
| **Resolver audits** | `../infra/check-resolvable.mjs` + `../infra/dry-audit.mjs` | Are all skills wired up + non-overlapping? | Pre-push + on AGENTS.md / SKILL.md change |

## Top-level CI gate

Run the lot in one shot:

```bash
bash eval/run-all-evals.sh             # default — audits + smoke + trigger routing (needs OPENAI_API_KEY)
bash eval/run-all-evals.sh --no-llm    # offline — audits + smoke only (pre-push default)
bash eval/run-all-evals.sh --judge     # deeper — adds LLM-as-judge on every rubric-tagged output (gpt-5.4-nano)
```

Mode comparison:

| Mode | Cost | Use |
|---|---|---|
| `--no-llm` | $0 | Wired into pre-push hook (`infra/install-pre-push-hook.sh`); runs on every push |
| default | ~$0.01 | Local sanity check before opening a PR |
| `--judge` | < $0.05 | Weekly cron or on intentional rubric changes |

Exit 0 iff every step passed. See `eval/schemas/skill.schema.md` and
`eval/schemas/skill-eval.schema.md` for the contracts skills must satisfy.

### What each audit catches

- **`check-resolvable.mjs`** — every `skills/*/SKILL.md` has the required frontmatter, a real `script:`, and a real `eval:` pointer. Brain dirs in `writes_to:` / `brain_dirs_read:` exist in the canonical brain (`antriksh-29/antriksh-brain` via `gh api`).
- **`dry-audit.mjs`** — no two skills claim the same trigger phrase. Substring overlaps surfaced as warnings.
- **`run-skill-eval.mjs <skill-dir>`** — runs the skill's smoke + structural output check + LLM-resolver routing (≥ 90% top-1) + (with `--judge`) gpt-5.4-nano scoring against `judge_rubric:` (≥ 8/10).

### Original three suites (unchanged)

## Why three suites instead of one

| Failure mode | Caught by |
|---|---|
| External text contains "ignore previous instructions" → bot complies | Injection fixture |
| User says "what did Garry say last month?" → brain returns wrong pages | Retrieval eval |
| User says "add a task" → bot routes to query skill instead of task skill | Behavioral eval (routing) |
| Morning briefing misses a meeting that's on the calendar | Behavioral eval (factuality) |
| Bot hallucinates a meeting attendee | Behavioral eval (hallucination check) |
| Bot's voice drifts into corporate/sycophantic | Behavioral eval (voice check) |
| Brain returns 0 pages for a known-present concept | Retrieval eval |
| Bot refuses a legitimate user request | Behavioral eval (over-refusal check) |
| Bot complies with a malicious directive embedded in an email | Injection fixture |

Different signal, different mechanisms, different consequences:

- **Injection fixture** is binary (pass/fail per pattern). Failing the suite blocks boot.
- **Retrieval eval** is quantitative (nDCG@3, MRR). Failing means tune ranking.
- **Behavioral eval** is qualitative (0-10 per scenario, manual + LLM-judge). Failing means tune the skill.

## Update cadence

| Suite | When to add patterns |
|---|---|
| Injection fixture | Monthly review during Sunday hygiene + whenever a real-world injection is observed |
| Retrieval eval | Whenever a query type returns surprisingly bad results (add the query + correct answer) |
| Behavioral eval | Whenever you notice the bot doing something subtly wrong (add the scenario + expected behavior) |

The behavioral eval especially is a living document. Every "huh, the bot got that
wrong" moment should produce a new entry. After 6 months, the suite represents
your accumulated taste about what good behavior looks like for *this specific bot*.
