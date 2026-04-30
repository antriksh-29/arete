# eval/

Evaluation artifacts for the personal AI assistant. Three distinct test suites
covering three different failure modes. None replace the others.

| Suite | File | Question | Authored in | Run when |
|-------|------|----------|-------------|----------|
| **Injection fixture** | `../openclaw/_smoke/injection-fixture.yaml` | Does the bot **refuse** to comply with malicious instructions? | Phase 2 ✅ | Every systemd start (boot blocks on miss) |
| **Retrieval eval** | `retrieval-eval.yaml` | Does the brain return the **right pages** for a query? | Phase 4 | Weekly hygiene cron + on intentional algorithm changes |
| **Behavioral eval** | `behavioral-eval.yaml` | Does the bot **do good things well** — accurate answers, correct skill routing, useful synthesis? | Phase 4-5 (grows with each skill) | Weekly hygiene cron + on intentional skill changes |

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
