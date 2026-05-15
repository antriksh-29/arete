# Model routing decisions

Records which model runs which task and why. Update on any change.

**This doc is documentation, not config.** The runtime sources of truth are:

- **OpenClaw main agent loop** (every Telegram turn, briefings, meeting
  prep): `~/.openclaw/openclaw.json` on the VPS — specifically
  `agents.defaults.model.{primary, fallbacks}` and
  `agents.defaults.models["<ref>"].params.thinking`. Managed
  idempotently by `infra/configure-openclaw.sh` — change knobs there
  and redeploy, do not hand-edit on the VPS.
- **Python scripts** (`bootstrap-enrich.mjs`, `ingest-pdf.py`,
  `web-search.sh`, etc.): `~/bin/llm.py` tier abstraction with
  hardcoded provider/model strings — edit `llm.py` to change tier
  mappings.

If you change either source of truth, update this doc in the same
commit. Drift between this doc and the live config has burned us
twice (2026-05-14 Kimi cutover, see `docs/KIMI-INTEGRATION-FINDINGS.md`).

---

## Active routing (2026-05-15 — Kimi K2.6 promoted to primary)

**Why the change:** Kimi K2.6 (256K context, strong tool-use, ~$0.60/$3.00
per 1M in/out tokens) is materially cheaper than the gpt-5.4-mini
primary for the agent's heavy-context Telegram path (AGENTS.md +
SKILL.md bootstrap ≈ 75K chars per turn). The 2026-05-14 cutover
attempt failed silently — config was switched but `params.thinking`
was never configured, so every Kimi turn hit thinking-mode generation
that blew the 180s gateway timeout and fell through to gpt-5.4-mini.
Resolved 2026-05-15 with `thinking: {type: "disabled"}` (instant mode).
Verified via Telethon E2E (5/5 pass, 6–21s round-trip, zero fallback
events).

### OpenClaw main-agent chain (Telegram, briefings, meeting prep)

| Position | Model | Notes |
|---|---|---|
| Primary | `moonshot/kimi-k2.6` | Instant mode (`params.thinking: {type: "disabled"}`). Sub-25s round-trip in practice. Auth via plaintext key in `openclaw.json` (hygiene follow-up: move to env). |
| Fallback 1 | `openai/gpt-5.4-mini` | Cross-provider cushion for Moonshot outage / rate-limit. Auth from gateway shell env (`OPENAI_API_KEY`). |
| Fallback 2 | `anthropic/claude-haiku-4-5` | Last resort if both above fail. Auth from `~/.openclaw/agents/main/agent/auth-profiles.json`. |

**Failover triggers:** rate-limit signals only (429, "quota", "resource
exhausted", etc). 5xx, network errors, and 400/401 do NOT trigger
auto-failover — they surface as request failures. See
`.context/attachments/kimi-k2.6-openclaw-config-deep-dive.md` §Gap 1.

**Per-agent overrides** (`agents.list[]` in `openclaw.json`):

- `main` → inherits the chain above (Telegram-facing).
- `signal-detector` → pinned to `openai/gpt-5.4-mini` (every user
  message captures to brain via this; cheap + fast required).
- `feedback-learner` → pinned to `openai/gpt-5.4-mini` (background
  retrospective analyser).

### Python-script tier chain (`~/bin/llm.py`)

Unchanged from 2026-05-04. These scripts do not currently route
through Kimi — they hit OpenAI / Anthropic directly via the tier
abstraction. Migrating tiers to Moonshot is a separate decision.

| Tier | Primary | Fallback (cross-provider) | Where used |
|---|---|---|---|
| **mini** | `openai/gpt-5.4-mini` | `anthropic/claude-haiku-4-5` | Default for all Python LLM calls |
| **full** | `openai/gpt-5.4` | `anthropic/claude-sonnet-4-6` | Reserved for tasks needing more reasoning than mini delivers (currently none — registered for future use) |
| **nano** | `openai/gpt-5.4-nano` | `anthropic/claude-haiku-4-5` | Reserved for narrow classification (currently none — see "deferred optimizations" below) |
| **vision** | `openai/gpt-5.4-mini` | `openai/gpt-5.4` | Image OCR / image understanding (`~/bin/image-ocr.py`) |

### Per-task routing

| Task | Tier | Why |
|---|---|---|
| Telegram main agent loop (every conversation, briefings, meeting prep, reading-list invocations, all skill dispatch) | mini | Strong tool-use, cheap, vision-capable when openclaw passes images through. |
| Daily Gmail/Calendar enrichment (`bootstrap-enrich.mjs`, `brain-daily-collect.sh`) | mini | Multi-step extraction with citation handling. |
| Web search (`~/bin/web-search.sh`) | mini | OpenAI's `web_search_preview` tool requires mini+. Not nano-eligible. |
| Essay-to-podcast script (`~/bin/generate-podcast-script.sh`) | mini | Long-form creative dialogue, persona maintenance. |
| PDF ingestion analysis (`~/bin/ingest-pdf.py`) | mini | Heavy synthesis over 22k+ word docs, 5-section structured output. |
| X thread analysis (`~/bin/ingest-x-thread.py`) | mini | Bundle reconstruction + analysis with [[wikilinks]]. |
| X corpus collection (`~/bin/x-corpus-collect.py` admired path) | mini | Web search tool needed. |
| X voice profile compile (`~/bin/x-style-profile.py`) | mini | Distills 6 corpora into structured fingerprints. |
| X tweet draft (`~/bin/x-draft.py`) | mini | Creative writing under voice + 280-char constraint. |
| Image OCR / image understanding (`~/bin/image-ocr.py`) | vision | New 2026-05-04. mini supports vision; full as fallback. |
| Reading-list URL kind detection | none | Pure regex. No LLM. |
| Reading-list title fetch (HTML / X) | none | HTML scrape + regex / X v2 API. No LLM. |
| Habit logging / water log | none | Deterministic shell. |
| ClawVisor's intent verifier | not ours | ClawVisor's internal choice. |
| Gbrain embeddings (every page write + every query) | not ours | `text-embedding-3-large`, hardcoded. ~$0.0001/page. |

---

## Cross-provider failover

Implementation: `~/bin/llm.py` (shared module). Every Python script that
talks to an LLM imports it and calls `llm.call(system_prompt, user_msg,
tier="...")`. The module:

1. Tries the tier's primary provider.
2. On HTTP 401/402/403/429/5xx OR network error → falls back to the
   cross-provider equivalent.
3. Returns `(text, provider_used)` so callers can log which provider
   answered.

The openclaw main agent gets the same behavior at the runtime layer:
its config has `primary: openai/gpt-5.4-mini` and `fallbacks:
[anthropic/claude-haiku-4-5]` in `~/.openclaw/openclaw.json`.

Verified 2026-05-04 by setting an invalid `OPENAI_API_KEY` and confirming
fallback to Haiku returned correct output.

---

## Deferred optimizations (nano candidates, intentionally NOT applied)

Honest audit on 2026-05-04: **no current task safely drops to nano
without quality regression.** All current mini workloads are either
heavy multi-step synthesis or tool-using.

Two refactor opportunities worth tracking (require splitting scripts
into classifier + extractor stages, not a model swap):

1. **`bootstrap-enrich.mjs` two-stage** — split "is this email
   signal-grade? yes/no" (nano-eligible) from "extract the timeline
   entry" (mini-required). Saves ~30% on Gmail-heavy days.
2. **Reading-list intent classifier** — if we ever add LLM-based
   intent detection ("save this for later" vs "what do you think"),
   nano fits the classifier role.

Don't blindly downgrade. Verified zero current tasks qualify.

---

## Lesson preserved: don't blame the model before reading the trajectory

On 2026-05-01, a Telegram failure (user asked about hydration, bot said
no logs while 2L was logged) was misdiagnosed as a Haiku problem. The
real cause was AGENTS.md ambiguity that led the model to call the wrong
tool. Trajectory file at
`~/.openclaw/agents/main/sessions/<uuid>.trajectory.jsonl` shows what
the model actually emitted. If the model emitted tool calls that
returned empty/failed, the bug is in your tool definitions, not the
model. If the model emitted no tool calls at all, THEN question the
model — but only after ruling out documentation gaps.

This lesson is still operative under the new GPT-5.4-mini-primary
routing. Provider may change, the diagnosis discipline does not.

---

## How to update this doc

Any time you:
- Change a model assignment for a task
- Add a new MCP server that exposes tools to the agent
- Add a new automation that involves an LLM call
- Hit a cost surprise

…update this file with the change and reasoning. It's the record of where
the cost is going.
