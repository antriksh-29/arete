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

## Active routing (2026-05-26 — DeepSeek v4 pro promoted to primary)

**Why the change:** DeepSeek v4 pro (1M context, Sonnet-tier benchmarks,
MoE 1.6T/49B active) launched 2026-04-24 with promo pricing at
$0.435/$0.87 per 1M tokens — roughly 5× cheaper than Kimi K2.6 for
comparable capability. Kimi K2.6 demoted to fallback-1 (still in the
chain because DeepSeek has reported peak-time 503s and a known
intermittent tool-call schema bug — DeepSeek-V3 issue #1244 — so the
cushion is load-bearing). Revert recipe preserved below in case the
quality / reliability profile turns out worse than expected.

**Risks to monitor:**
- DeepSeek-V3#1244: intermittently emits `tool_calls` as plain text in
  `content`. Non-deterministic. Material for `signal-detector` and any
  skill that drives tools.
- Peak-time 503s reported by multiple operators on HN / WaveSpeed.
- Structured outputs (strict JSON schema): not verified — only loose
  JSON mode confirmed. Don't migrate strict-schema callers yet.
- v4 pro is TEXT-ONLY — vision tier explicitly keeps DeepSeek out.

### OpenClaw main-agent chain (Telegram, briefings, meeting prep)

| Position | Model | Notes |
|---|---|---|
| Primary | `deepseek/deepseek-v4-pro` | Instant mode (`params.thinking: {type: "disabled"}`). Routes via deepseek-proxy at `127.0.0.1:18791` (circuit breaker + first-line failover to moonshot-proxy). |
| Fallback 1 | `moonshot/kimi-k2.6` | Instant mode. Routes via moonshot-proxy at `127.0.0.1:18790`. Covers DeepSeek tool-call schema bug + peak-time 503s. |
| Fallback 2 | `anthropic/claude-haiku-4-5` | Cross-provider cushion. Auth from `~/.openclaw/agents/main/agent/auth-profiles.json`. |
| Fallback 3 | `openai/gpt-5.4-mini` | Last resort. **Risk**: OpenClaw session-format bug with `reasoning` items — session-rotator service is the structural mitigation. If we see "400 Item 'msg_X' was provided without its required 'reasoning' item", drop this link first before touching anything else. |

**Failover triggers** (OpenClaw native): rate-limit signals (429, "quota",
"resource exhausted"). Both proxies ALSO catch 5xx / network errors /
overload-shaped error bodies that OpenClaw's native trigger misses.

**Per-agent overrides** (`agents.list[]` in `openclaw.json`):

- `main` → inherits the chain above.
- `signal-detector` → **pinned** to `deepseek/deepseek-v4-pro` (per
  user 2026-05-26). **WARNING**: bare-string override has no fallback
  chain — if the DeepSeek tool-call schema bug fires here, every
  Telegram turn dies. First thing to revert at any sign of trouble.
- `feedback-learner` → pinned to `deepseek/deepseek-v4-pro` (background
  retrospective analyser; lower blast radius than signal-detector).

### Thinking-mode knob

Default is `disabled` (instant) on the deepseek-proxy + the OpenClaw
model block, matching Kimi discipline that prevents 180s gateway
timeouts. If you ever want to try thinking mode (slower but possibly
better reasoning):

```bash
systemctl --user edit deepseek-proxy.service
# Add:
#   [Service]
#   Environment=DEEPSEEK_THINKING=enabled
#   Environment=DEEPSEEK_REASONING_EFFORT=medium
systemctl --user restart deepseek-proxy.service
```

Watch round-trip latency in the gateway journal. Revert by removing
the override or setting `DEEPSEEK_THINKING=disabled`.

### Python-script tier chain (`~/bin/llm.py`)

**Updated 2026-05-26** to walk an N-link chain (was 2-tuple). Same
DeepSeek-led order as OpenClaw for `mini`/`full`. Vision tier
explicitly excludes DeepSeek.

| Tier | Chain (primary → fallbacks) | Where used |
|---|---|---|
| **mini** | `deepseek/v4-pro` → `moonshot/k2.6` → `anthropic/haiku-4-5` → `openai/gpt-5.4-mini` | Default for all Python LLM calls |
| **full** | `deepseek/v4-pro` → `moonshot/k2.6` → `anthropic/sonnet-4-6` → `openai/gpt-5.4` | Heavy reasoning (currently none — registered) |
| **nano** | `openai/gpt-5.4-nano` → `anthropic/haiku-4-5` | Reserved for narrow classification (currently none). Intentionally NOT on the DeepSeek chain — keeps experiment surface contained. |
| **vision** | `moonshot/k2.6` → `anthropic/haiku-4-5` → `openai/gpt-5.4-mini` | Image OCR / image understanding. DeepSeek v4 pro is text-only and is rejected with a hard error if a caller passes images to it. |

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

## Revert recipe (if the DeepSeek experiment fails)

Restore the 2026-05-15 Kimi-primary configuration:

1. **OpenClaw config** — edit `infra/configure-openclaw.sh`:
   - `agents.defaults.model.primary` → `"moonshot/kimi-k2.6"`
   - `agents.defaults.model.fallbacks` → `["openai/gpt-5.4-mini", "anthropic/claude-haiku-4-5"]`
   - Remove the `deepseek/deepseek-v4-pro` model block
   - `PER_AGENT_MODEL` → both back to `"moonshot/kimi-k2.6"`
2. **Python tiers** — edit `infra/llm.py` `_CHAINS`:
   - Restore the 2-tuple-equivalent chains documented in the file header.
3. **Proxy** — leave `deepseek-proxy.service` installed but stopped:
   `systemctl --user disable --now deepseek-proxy.service`. The systemd
   unit + script stay in the repo so re-enabling for a future
   experiment is one `systemctl enable --now` away.
4. **OpenClaw provider block** — leave `models.providers.deepseek` in
   `openclaw.json` (harmless without active routing) or strip it.
5. **Redeploy** — `bash infra/deploy-vps.sh && bash infra/configure-openclaw.sh && systemctl --user restart openclaw-gateway`.

Verification: send a Telegram message, confirm gateway journal logs
the route as `moonshot/kimi-k2.6`. E2E harness should pass 5/5.

## Provisioning checklist for the DeepSeek experiment

Before first deploy of this experiment:

1. **Add the key to the vault** (on the VPS):
   ```bash
   bot-cred set DEEPSEEK_API_KEY 'sk-...'
   ```
   The proxy launcher reads it from there. Do NOT put the key in any
   tracked file.
2. **Add provider block to `~/.openclaw/openclaw.json`** (one-time, NOT
   managed by configure-openclaw.sh because it contains the API key):
   ```json
   "models": {
     "providers": {
       "deepseek": {
         "apiKey": "sk-...",
         "baseUrl": "https://api.deepseek.com/v1"
       }
     }
   }
   ```
   `deploy-vps.sh` will then rewrite `baseUrl` to
   `http://127.0.0.1:18791/v1` so all calls go through the proxy.
3. **Verify proxy starts**: `systemctl --user status deepseek-proxy.service`
   should show active. `curl http://127.0.0.1:18791/v1/chat/completions`
   with a small payload should return a real response.
4. **Rotate the key** — the one shared during planning was sent over
   chat. Replace it with a fresh key from the DeepSeek console before
   running real workloads.

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
