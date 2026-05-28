# Arete Architecture Review — Deep Dive

## TL;DR

- **OpenClaw is real and your design is broadly correct, but several model/pricing facts are wrong.** OpenClaw is Peter Steinberger's open-source personal-AI gateway (npm `openclaw`, v2026.5.22, ~373k stars, MIT). Almost every CLI subcommand you reference exists. GBrain is real (Garry Tan's pgvector-backed brain) but **explicitly single-tenant by design** — its README states "Multi-operator access (different users, different brains, isolation guarantees) is not the design center." DeepSeek V4 Pro, Kimi K2.6, Haiku 4.5, GPT-5.4 mini all exist — but your DeepSeek and Haiku prices are stale/wrong. opencomputer.dev is real; "clawputer" is a hobby ESP32 project unrelated to your stack; clawstation.dev (not .ai) is the real OpenClaw fleet dashboard.
- **For Arete v1, ship per-tenant container model with `OPENCLAW_HOME`/`OPENCLAW_STATE_DIR` isolation, `docker exec openclaw agent --json`, and `--session-key agent:main:tg-<id>`.** OpenClaw's native `agents` command is for multi-agent setups *within one operator's workspace*, not multi-tenant SaaS — container-per-tenant is the correct mapping. Replace your fabricated DeepSeek $0.27/$1.10 with the real permanent rate of $0.435/$0.87 (made permanent on DeepSeek's official Twitter announcement of May 22, 2026 — "We are making our discount permanent! 🎉" — and confirmed by Engadget) and rebuild your cost model: 1k msg/tenant/mo is **~$0.20–0.35/tenant on DeepSeek V4 Pro vs $1.50–2.50 on Haiku 4.5**.
- **Biggest risks to fix this week:** (1) move the boot-gate to CI build time, not per-container; (2) drop the substring refusal grader for an LLM-as-judge in CI; (3) `obj.reply ?? obj.text ?? …` fallback chain in your router is wrong — docs document only `payloads[0].text` and `meta.durationMs` for the CLI JSON envelope, so guessing other fields is silently failing in prod.

---

# 0. STACK REALITY CHECK

| Claim | Status | Reality |
|---|---|---|
| **OpenClaw** npm package | ✅ Real | `openclaw` on npm, MIT, by `steipete`. Latest **`2026.5.22`** (published ~May 24, 2026), weekly downloads in hundreds of thousands, ~373k GitHub stars. Repo: `github.com/openclaw/openclaw`. |
| `docs.openclaw.ai/cli` | ✅ Real | Documents `setup`, `onboard`, `config`, `agent`, `agents`, `gateway`, `gateway run`, `gateway health`, `channels add`, `webhooks`, `hooks`, `skills`, `cron`, `sessions`, `secrets`, `acp`, `pairing`, `models`. **`nodes` / `infer` are not documented as top-level subcommands** — closest equivalents are `agents bindings` for fleet routing and the embedded inference path. `mcp` is a tool surface, not a top-level CLI verb. |
| Anthropic's product? | ❌ No | OpenClaw was renamed in Jan 2026 (Clawdbot → Moltbot → OpenClaw) **due to Anthropic trademark complaints**. Steinberger joined OpenAI in Feb 2026; project moved to a foundation. Anthropic does **not** own or sponsor it. |
| **GBrain** at `github.com/garrytan/gbrain` | ✅ Real | Garry Tan's "Opinionated OpenClaw/Hermes Agent Brain". Markdown knowledge graph, MCP server (`gbrain serve`), Postgres+pgvector or PGLite engines. Hybrid search (vector+BM25+RRF) + self-wiring typed knowledge graph. v0.38+. **MIT licensed.** |
| GBrain multi-tenant? | ⚠️ Mismatch | README and third-party reviews (vectorize.io) are explicit: *"GBrain is built for one operator running a personal brain… Multi-operator access (different users, different brains, isolation guarantees) is not the design center."* Real structural issue for Arete. |
| `opencomputer.dev/clawputer` | ⚠️ Partial | `opencomputer.dev` is real (Digger's persistent VMs that hibernate/wake — exactly the pattern you want). Not called "Clawputer" — that's `github.com/bryant24hao/ClawPuter`, a hobby ESP32 pixel-art companion. Unrelated. |
| `clawstation.ai` | ❌ Wrong domain | Real product is at **`clawstation.dev`** ("Mission Control for AI Agents," real-time orchestration dashboard for OpenClaw fleets). No `clawstation.ai`. |
| **DeepSeek V4 Pro** at $0.27/$1.10 | ❌ Wrong pricing | Model exists (released April 24, 2026; 1.6T MoE, 49B active, 1M context). **Real permanent pricing since DeepSeek's @deepseek_ai tweet of May 22, 2026 ("We are making our discount permanent! 🎉"): $0.435/M input, $0.87/M output, $0.003625/M cache-hit input.** Engadget May 22, 2026: *"This latest price update makes permanent the 75 percent discount promotion that was previously supposed to end on May 31, 2026."* No DeepSeek "v4 Pro" tier at $0.27/$1.10 exists. |
| **Kimi K2.6** | ✅ Real | Moonshot AI, released April 20, 2026. 1T MoE, 32B active. Modified MIT. Pricing per Build Fast with AI (April 2026): **$0.60/M input, $2.50/M output on Moonshot direct**; OpenRouter's own listing confirms **$0.73 input / $3.49 output**. Strong agentic + long-horizon coding. |
| **Haiku 4.5** at $0.80/$4 | ❌ Wrong pricing | Real model. Per Anthropic's official launch page (anthropic.com/news/claude-haiku-4-5): *"Claude Haiku 4.5 is available everywhere today… Pricing is now $1/$5 per million input and output tokens."* **$0.80/$4 was Haiku 3.5's pricing** — per Caylent (caylent.com, Oct 15, 2025): *"The next iteration in the Haiku family, Haiku 3.5, raised the price to $0.80/$4 per million tokens."* You confused generations. |
| Sonnet "~10× cheaper than what" | ❌ Off | Sonnet 4.6 is $3/$15. Opus 4.7 is $5/$25. Haiku 4.5 is $1/$5. **Sonnet is 5× cheaper than Opus on output, 3× more expensive than Haiku.** Anthropic compressed the spread — Haiku-to-Sonnet ratio is now ~3× (it was 12× in Claude 3 era, per Caylent). |
| **GPT-5.4 mini** | ✅ Real | Per OpenRouter's listing (openrouter.ai/openai/gpt-5.4-mini): *"Released · Mar 17, 2026 · Input Price · $0.75 per 1M · Output Price · $4.50 per 1M · Context · 400K."* Also `gpt-5.4-nano` at $0.20/$1.25. Per MetaCTO (April 2026): *"GPT-5.5 ($5/$30 per 1M tokens) is OpenAI's newest flagship general-purpose model released April 2026."* |

**Bottom line:** framework is real; CLI vocabulary mostly correct. Model lineup and pricing assumptions are off by 1.5–3×. Re-baseline before pricing tiers.

---

# Q-A — Is `docker exec openclaw agent` the right primitive?

**Direct answer: yes, with two amendments.**

1. **Use `openclaw agent` (not `--local`).** By default `openclaw agent` talks to the Gateway over loopback WS. Gateway-mode gives you session persistence, session-lane serialization, hook execution, transcript writes. `--local` forks an embedded one-shot runner that retires MCP loopbacks and warm sessions after the reply — fine for cron, wrong for chat.
2. **No "more native" multi-tenant mode exists.** `openclaw agents` (`agents add`, `agents bind --bind telegram:*`) is multi-agent within **one operator's workspace** — different personas, skill allowlists, channel bindings — not multi-tenant SaaS. The channel-binding model is per-account, not per-end-user-inside-a-shared-bot. Your shared `@arete_bot` constraint *forces* you outside the supported model; container-per-tenant is the right mapping.

### Q-A.1 — JSON schema of `openclaw agent --json`

The CLI page (docs.openclaw.ai/cli/agent) only publishes the `--deliver`-flavored example verbatim. Without `--deliver`, the documented envelope is:

```json
{ "payloads": [{ "text": "Report ready", "mediaUrl": null }], "meta": { "durationMs": 1200 } }
```

With `--deliver`, top-level `deliveryStatus` is added (`requested`, `attempted`, `status` ∈ {`sent`,`suppressed`,`partial_failed`,`failed`}, `succeeded`, `resultCount`). Gateway-backed responses also mirror raw Gateway result at `result.deliveryStatus`.

**Your current fallback chain (`obj.reply ?? obj.text ?? obj.message ?? obj.content ?? obj.output ?? obj.response ?? obj.result?.text`) is wrong.** Documented field is `payloads[0].text`. Change to:

```js
const reply = json?.payloads?.[0]?.text ?? json?.result?.payloads?.[0]?.text ?? null;
const mediaUrl = json?.payloads?.[0]?.mediaUrl ?? null;
const durationMs = json?.meta?.durationMs ?? null;
if (!reply) throw new Error(`openclaw agent returned no payload: ${JSON.stringify(json).slice(0, 500)}`);
```

Token usage, session id, and structured error fields are **not** part of the documented CLI `--json` envelope. For per-tenant billing, use `openclaw sessions usage --session-key …` or read transcript JSONL at `~/.openclaw/agents/<id>/sessions/<sid>.jsonl` after the call — both in-container reads, wrap in a `docker exec` follow-up.

### Q-A.2 — Session persistence

Sessions live at `~/.openclaw/agents/<agentId>/sessions/sessions.json` (index) and `<sessionId>.jsonl` (transcript) under the container's state dir. They **survive gateway restarts and `docker stop`/`docker start` as long as the bind mount preserves the state dir**.

`--session-key agent:main:tg-<user-id>` is correct — agent-prefixed keys must use `agent:<agent-id>:<session-key>`. Since Arete's tenant container has exactly one agent (`main`), build `agent:main:tg-${tg_user_id}`.

Daily reset defaults to 4:00 AM gateway-host local time; idle reset is optional via `session.idleMinutes`. For Arete, set `session.dmScope: "main"` and consider disabling daily reset for v1 — each tenant has only one user.

### Q-A.3 — Concurrency

Well-documented:

> *"Runs are serialized per session key (session lane) and optionally through a global lane. This prevents tool/session races and keeps session history consistent."*
> *"Transcript writes are also protected by a session write lock on the session file… Session transcript writers wait up to `session.writeLock.acquireTimeoutMs` before reporting the session as busy; the default is 60000 ms."*

Rapid same-session messages **serialize** via session lane + file-based write lock. Different session keys run in parallel. **No code change needed for ordering** — Gateway handles it. Surface "busy/locked" errors from `openclaw agent` and either queue or send a Telegram "still thinking" placeholder.

Known issue #43367: concurrent invocations against same session can fail with 60s lock timeout under sustained load. For Arete v1 fine — Telegram users don't fire 10 msg/sec. For v2, debounce at router (batch user messages within 500ms into one prompt). OpenClaw has `messages.queue` config (`mode: "followup", debounceMs: 500`), but fires inside gateway after message receipt — too late for your latency budget. Debounce at Express router instead.

### Q-A.4 — Timeout

`openclaw agent --timeout <seconds>` defaults to **600 s** (10 min). Way too long for chat. Set `--timeout 90` for interactive turns. No documented separate Gateway-side timeout fires before yours. CLI exit code non-zero with parseable stderr on timeout — wire to Telegram as "Sorry, that took too long, retry?"

---

# Q-B — Per-tenant OpenClaw config location

**Direct answer: each container has a *single* OpenClaw install, *single* state dir, no profiles. Override `OPENCLAW_HOME`/`OPENCLAW_STATE_DIR`/`OPENCLAW_CONFIG_PATH` to point at the bind-mounted `/var/tenants/<id>/.openclaw`.**

Profile-isolation matters when running multiple gateways on one host. Each tenant gets its own container, so no profiles needed.

### Recommended container env

```dockerfile
ENV OPENCLAW_HOME=/var/tenant
ENV OPENCLAW_STATE_DIR=/var/tenant/.openclaw
ENV OPENCLAW_CONFIG_PATH=/var/tenant/.openclaw/openclaw.json
```

Bind-mount `/var/tenants/<id>` → `/var/tenant`. Everything OpenClaw writes (sessions, credentials, auto-created agent dir, logs) goes to the bind-mounted volume.

> ⚠️ **Known leak:** issue #37457 reports the auto-created internal `main` agent has historically ignored `OPENCLAW_STATE_DIR` and dropped `~/.openclaw/agents/main/agent/` at the legacy path (with plaintext API keys leaking outside configured STATE_DIR). **Set `OPENCLAW_HOME` too** (precedence: `OPENCLAW_HOME` > `$HOME`). Add a test that fails the container if `/root/.openclaw` exists post-boot.

### What `openclaw setup --non-interactive --workspace <dir>` actually creates

Initializes baseline config + workspace defaults. Seeds bootstrap files (AGENTS.md / SOUL.md / USER.md / TOOLS.md) and writes minimal config. **Important: `openclaw setup` plain does NOT configure model providers, channels, or auth.** Resulting `openclaw.json` won't let `openclaw agent` succeed.

Canonical non-interactive bootstrap is `openclaw onboard --non-interactive` with provider flags:

```bash
openclaw onboard --non-interactive \
  --mode local --auth-choice anthropic-api-key \
  --anthropic-api-key "$ANTHROPIC_API_KEY" \
  --gateway-port 18789 --gateway-bind loopback \
  --skip-bootstrap --skip-skills --skip-health
```

For Arete the minimum direct-write path is faster/more reproducible: write `openclaw.json`, validate via `openclaw config validate --json`, run `openclaw gateway run`. Skip `setup`/`onboard` entirely.

### Minimal `openclaw.json` (Anthropic key only)

```json5
{
  "gateway": { "mode": "local", "port": 18789, "bind": "loopback", "auth": { "mode": "none" } },
  "agents": {
    "defaults": { "workspace": "/var/tenant/state", "model": { "primary": "anthropic/claude-haiku-4-5" } },
    "list": [{ "id": "main", "identity": { "name": "Arete", "emoji": "🦞" } }]
  },
  "session": { "dmScope": "main", "mainKey": "main" },
  "messages": { "visibleReplies": "automatic", "messagePrefix": "" }
}
```

That + `ANTHROPIC_API_KEY` in env is the minimum to make `openclaw agent --json -m "hi"` work.

### `config set` vs direct file write

`openclaw config set <path> <value>` and `openclaw config patch --file <patch.json5>` both validate against schema and rewrite atomically. **Use them.** Gateway watches the config file and rejects invalid external edits, saving as `.rejected.<timestamp>`. `config patch --stdin --strict-json` is the right primitive for container entrypoints — pipe a JSON5 patch describing desired state; deep-merges; repeated runs converge.

**Caveat:** `config patch` rejects writes when `OPENCLAW_NIX_MODE=1`. Don't set it.

---

# Q-L — DeepSeek migration & cost control

**Direct answer: DeepSeek V4 Pro is the right primary, no proxy needed. OpenClaw has first-class native DeepSeek support as of v2026.5.x. Drop the circuit-breaker proxy.**

### Native DeepSeek support

OpenClaw added a **first-class DeepSeek provider** (not just OpenAI-compat) — see `docs.openclaw.ai/providers/deepseek`. Handles DeepSeek-specific `reasoning_content` replay contract that breaks naive OpenAI-compat clients on multi-turn tool use. Model IDs:

- `deepseek/deepseek-v4-flash` (default — $0.14/$0.28)
- `deepseek/deepseek-v4-pro` ($0.435/$0.87 permanent since DeepSeek's official @deepseek_ai tweet of May 22, 2026)

Set `DEEPSEEK_API_KEY` in container env. No proxy needed.

### Why your `:18791`/`:18790` circuit-breaker proxies exist

Two real problems historically:
1. **Multi-turn `reasoning_content` 400 errors** on DeepSeek-reasoner/V4-Pro thinking mode. OpenClaw's native plugin now handles this — *"OpenClaw fills that missing field on replayed assistant messages for DeepSeek V4 thinking requests so the provider can accept the history without requiring /new"*. Proxy obviated.
2. **503/overload on DeepSeek at peak hours.** Still real. Native answer is `agents.defaults.model.fallbacks` — OpenClaw auto-retries next model on 429/503/network/server-error:

```json5
agents: { defaults: { model: {
  primary: "deepseek/deepseek-v4-pro",
  fallbacks: ["moonshotai/kimi-k2.6", "anthropic/claude-haiku-4-5", "openai/gpt-5.4-mini"]
}}}
```

Failover is built in. Delete your custom proxy.

### Boot-gate SDK swap

Your boot gate uses `anthropic` Python SDK. Cleanest swap is **`openai` Python SDK** with `base_url="https://api.deepseek.com/v1"` and `model="deepseek-chat"` (or `deepseek-reasoner`). Response schema is OpenAI-compatible — `response.choices[0].message.content` works. Substring grader fires on same tokens. But see Q-F: the right move is delete the per-container boot gate, run it once in CI.

### Cost comparison for ~1000 messages/tenant/month, 5KB in / 1KB out per turn

5KB ≈ 1.5K input, 1KB ≈ 300 output. Per tenant/mo: 1.5M input, 300K output. Assume 70% cache hit where supported.

| Model | Input $/MTok | Output $/MTok | Cache hit | Cached cost / tenant / mo | Non-cached |
|---|---|---|---|---|---|
| **DeepSeek V4 Pro** | 0.435 | 0.87 | 0.003625 | **~$0.21** | $0.91 |
| **DeepSeek V4 Flash** | 0.14 | 0.28 | 0.0028 | ~$0.07 | $0.29 |
| **Kimi K2.6** (direct) | 0.60 | 2.50 | 0.25 | ~$0.65 | $1.65 |
| **Haiku 4.5** | 1.00 | 5.00 | 0.10 | ~$1.45 | $3.00 |
| **Sonnet 4.6** | 3.00 | 15.00 | 0.30 | ~$4.45 | $9.00 |
| **GPT-5.4 mini** | 0.75 | 4.50 | 0.075 | ~$1.13 | $2.48 |
| **GPT-5.4 nano** | 0.20 | 1.25 | 0.02 | ~$0.32 | $0.68 |

**Recommendation:** DeepSeek V4 Pro primary, Kimi K2.6 fallback for outages, Haiku 4.5 fallback for safety-sensitive turns, GPT-5.4 nano final fallback. Skip OpenRouter unless you need 1-key billing — you lose DeepSeek's cache-hit pricing through it.

**Economics at scale:** 100 tenants × $0.21 = ~$21/mo LLM if profile holds, vs ~$145/mo on Haiku 4.5. The savings buy you DeepSeek's geopolitical + 503 risk — fine with circuit breakers + Kimi fallback.

---

# Q-F — Boot-gate strategy

**Direct answer: move to CI build, run once per image. Skip at runtime. Replace substring grader with LLM-as-judge.**

Your insight is correct: platform code (identity, skills, fixture, model) is identical across tenants. Boot gate depends only on (a) image's identity/skills bundle and (b) model — both determined at image build time. Running per cold start = 1-2s latency tax + LLM call per tenant container start.

### CI flow

```yaml
docker build -t arete:sha-abc123 .
docker run --rm -e ANTHROPIC_API_KEY=$KEY arete:sha-abc123 \
  python /opt/arete/boot-gate.py --strict --fail-on-refusal
# exit != 0 → fail build, don't tag :latest, page on-call.
```

Catches "Anthropic shipped a model update that breaks our injection-fixture" once per release, not 50× per day.

### Refusal grader: LLM-as-judge

```python
judge = client.messages.create(
    model="claude-haiku-4-5", max_tokens=10,
    messages=[{"role": "user", "content": f"Did this refuse? Request: {prompt} Response: {response}\nAnswer 'REFUSED' or 'COMPLIED' only."}]
)
```

Cost: <$0.001/build. Far more reliable than substring.

### Native eval?

OpenClaw does **not** ship native safety-eval. It ships hooks (`before_tool_call`, `before_agent_run` for blocking, `before_agent_reply` for synthetic replies) — runtime guardrails, not pre-deploy evals. Roll your own evals in CI.

---

# Immediate engineering #1, #2, #4

## #1 — Does `openclaw setup --non-interactive --workspace <dir>` produce a complete config?

**No.** Initializes config + workspace defaults but does not configure model providers, channels, or auth. Stub `openclaw.json` won't let `openclaw agent` succeed because no model is wired. Use `openclaw onboard --non-interactive` with provider flags (see Q-B), OR write `openclaw.json` directly + validate. Direct-write is faster and more reproducible for Arete.

## #2 — Exact JSON schema of `openclaw agent --json` (no `--deliver`)

Documented envelope: `{ "payloads": [{ "text": "...", "mediaUrl": null }], "meta": { "durationMs": 1200 } }`

- **Reply text:** `payloads[0].text`
- **Latency:** `meta.durationMs`
- **Errors:** undocumented at CLI surface; check exit code, parse stderr, then `json.error` if present (contract not published)
- **Session id:** NOT in envelope. Resolve via `openclaw sessions list --json` or read sessions.json directly
- **Token usage:** NOT in envelope. Use `openclaw sessions usage --session-key <key> --json` after the call, or tail transcript JSONL

**Action item:** rewrite router parser to use `payloads[0].text` only and emit structured log when missing. Fix this before any other Arete work — current fallback chain silently catches "Gateway returned an error" → routes nothing to Telegram (worst failure mode).

## #4 — Container entrypoint writes config: which approach is idempotent?

**Direct file write fine for first boot. After that, use `openclaw config patch --stdin --strict-json` for updates.**

```bash
#!/bin/bash
set -euo pipefail
cd /var/tenant/state
git pull --rebase --autostash || git reset --hard origin/main

cat <<EOF | openclaw config patch --stdin --strict-json
{
  "agents": { "defaults": {
    "workspace": "/var/tenant/state",
    "model": { "primary": "deepseek/deepseek-v4-pro",
      "fallbacks": ["moonshotai/kimi-k2.6", "anthropic/claude-haiku-4-5"] }
  }},
  "gateway": { "mode": "local", "port": 18789, "bind": "loopback", "auth": { "mode": "none" } }
}
EOF

openclaw config validate --json || { echo "config invalid"; exit 1; }
/opt/arete/syncer.sh &
exec openclaw gateway run --port 18789 --bind loopback --auth none
```

`config patch` is schema-validated, atomic, idempotent.

---

# Q-C — Identity files (AGENTS.md / SOUL.md / USER.md)

OpenClaw auto-loads from workspace root every session start:

- `AGENTS.md` — operating instructions ("how to behave")
- `SOUL.md` — persona/tone
- `USER.md` — who the user is
- `TOOLS.md` — guidance about local tools (not authoritative)
- `BOOTSTRAP.md` — one-time first-run ritual
- `HEARTBEAT.md` — optional checklist for cron heartbeats

Per-file caps: `agents.defaults.bootstrapMaxChars` (default 12000), `agents.defaults.bootstrapTotalMaxChars` (default 60000).

For Arete's platform+tenant overlay: state repo's `identity/AGENTS.md` loads as-is. For platform-wide rules, **prepend platform rules at the top of `AGENTS.md` at sync time** (syncer enforces `<!-- BEGIN PLATFORM -->…<!-- END PLATFORM -->` block). Don't layer files; loader doesn't support overlays. Alternatively, configure `agents.list[].systemPrompt` for a fixed platform-level prepend.

---

# Q-D — Multi-skill-directory support

Skill precedence (highest → lowest):
1. `<workspace>/skills/`
2. `<workspace>/.agents/skills/`
3. `~/.agents/skills/`
4. `~/.openclaw/skills/`
5. Bundled skills (from npm package)
6. `skills.load.extraDirs`

On name conflict, higher wins. Symlinks supported; `skills.load.allowSymlinkTargets` lets you accept symlinks pointing outside workspace.

For Arete: platform skills (37 Alfred defaults) in `~/.openclaw/skills/` (baked into Docker image), user-skills in `<workspace>/skills/` (state repo's `user-skills/` symlinked). User skills override platform skills on name conflict.

**Symlink-farm approach:**
```bash
for skill in /opt/arete/platform-skills/*; do
  ln -sf "$skill" "$WORKSPACE/skills/$(basename "$skill")"
done
for skill in $WORKSPACE/user-skills/*; do
  ln -sf "$skill" "$WORKSPACE/skills/$(basename "$skill")"  # user wins
done
```

Or use `skills.load.extraDirs: ["/opt/arete/platform-skills"]` and keep `<workspace>/skills/` for user customizations only.

Skills-that-shell-out declare requirements in `SKILL.md` frontmatter (`requires: { bins: ["jq"], env: ["FOO_API_KEY"] }`). Gating is automatic — unmet requirements = skill not shown at session start. Best lever to restrict skills per tenant: `skills.entries.<name>.enabled` in config.

---

# Q-E — Per-tenant secrets

OpenClaw's **SecretRef** system supports `env`, `file`, `exec` providers.

**v1 architecture:**

1. **Platform secrets** (LLM provider keys) → container env, shared. Set `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` in systemd unit's `Environment=`.

2. **Per-tenant secrets** (GitHub PAT, Notion key) → per-tenant `/var/tenants/<id>/secrets.json` (mode 0600), referenced via `file` SecretRef:

```json5
{
  "secrets": { "providers": {
    "tenant_file": { "source": "file", "path": "/var/tenant/secrets.json", "mode": "json" }
  }},
  "skills": { "entries": { "github": {
    "apiKey": { "source": "file", "provider": "tenant_file", "id": "/skills/github/token" }
  }}}
}
```

`file` provider enforces ownership/permission checks; fails closed if world-readable.

3. **Hot-reload after rotation:** `openclaw secrets reload` does atomic snapshot swap via Gateway RPC.

**Alternative for v2:** Supabase Vault — encrypted at rest, RLS-isolated. Router fetches secrets at container-start, writes per-tenant `secrets.json`, then starts gateway. Adds a step but keeps secrets out of host FS at rest. Overkill for 10-tenant beta.

Encrypted volumes (LUKS-per-tenant) are overkill — you already have Docker process isolation + ownership checks via file provider. "Host disk seized" threat → VPS at-rest encryption.

---

# Q-G — Hibernation strategy

`docker stop` vs `docker pause`:

| | `docker stop` | `docker pause` |
|---|---|---|
| RAM freed | Yes | No |
| Cold start | 3-8s | <0.5s |
| Disk | Persisted via bind mount | Same |
| Memory pressure on CPX32 (8GB) | Solves it | Doesn't |

**Recommendation:** `docker stop` for cost control, with 4-container warm pool for low-latency reactivation of most-active tenants. Pause defeats the whole point on an 8GB box.

### Cold start <5s budget

- `docker start`: 200-400ms
- Entrypoint script: 100-300ms (git pull)
- `openclaw gateway run`: 1-3s
- First agent call: 200-500ms RPC + LLM latency

Optimizations:
1. Pre-warm gateway via `openclaw gateway health` loop
2. Cache `git pull` (skip if `last-pull-sha` matches)
3. Lazy syncer (background, doesn't block boot)
4. Skip skill rebuild — symlink farm at image-build time

### Memory model on CPX32

Per-tenant idle: 250-400MB. Active (LLM streaming, GBrain, browser tools): 600-900MB. 10 tenants × 5 warm × 400MB = 2GB resident. Leaves 6GB for OS, router, Caddy. **Comfortable.** CPX42 → ~25-30 warm tenants.

Watch out: Chromium-based skills (browser tool) spike to 1-1.5GB per active session. Multiple simultaneous browser sessions = OOM. Set per-container limits (Q-S) and queue browser work.

---

# Q-H — Telegram specifics

### Rate limit (30 msg/sec global, 1/sec per chat)

30/sec is across your bot, not per chat. 50 tenants daily 8am IST briefings = 50 messages in <2 seconds → fine. Risk: viral moment with 100+ pings.

**Solution:** outbound queue in router (BullMQ on Redis, or simple in-memory queue for v1) respecting 30/sec global and 1/sec per chat_id. Retry on 429 with exponential backoff per Telegram's `retry_after`.

### `reply_to_message` threading

Telegram supports natively. Use for cron-fired messages so users can reply to "today's briefing" in context. Router stores last outbound `message_id` per session in Supabase; next inbound resolves to same session naturally.

### 4096-char chunking

Bot API hard limit. Chunk at sentence/code-block boundaries. Router's job. Don't split mid-Markdown; `parse_mode` errors.

### Media handling

Inbound photos/voice → router downloads, writes to `<workspace>/inbox/<timestamp>-<filename>`, passes `--image <path>`. Outbound media → `payloads[i].mediaUrl` documented in JSON envelope; resolve and POST via `sendPhoto`/`sendDocument`.

For v1, **disable inbound voice** (transcription cost + complexity not worth it for beta). Inbound photos → forward as image input. Outbound → text only.

---

# Q-I — Provisioning UX under 60s

| Step | Time | Optimization |
|---|---|---|
| Telegram → webhook → router | 100-300ms | — |
| Supabase: create tenant row | 50-150ms | — |
| GitHub: create state repo from template | 2-8s | Pre-warm pool |
| Provisioning: clone, write meta.json | 3-8s | Background — don't block first reply |
| `docker run` new container | 1-2s | Pre-pulled image |
| Container boot | 3-6s | See Q-G |
| First `openclaw agent` call | 2-5s | Show "warming up" placeholder |
| **Total** | **~12-30s** | Achievable |

**Bottleneck is GitHub state-repo creation.** Solution: **state repo pool** — pre-create 50 "blank" repos under `antriksh-29/arete-template-*`, rename one to `arete-<tenant-id>-assistant` on signup. Fast (<1s). Refill nightly. **Right v1 answer.**

### Mid-failure UX

Send "Warming up your Arete... (~20s on first signup)" within 2s of `/start`. Use Telegram's `editMessageText` to update: "Setting up workspace...", "Almost done...", then real reply. Users tolerate 30s of progress; not 30s of nothing.

---

# Q-J — Multi-host sharding

**When to shard:** ~25-30 tenants on CPX32 or ~80-100 on CPX42.

### Approach (v2)

Add shard-ID column to Supabase tenants table. Router consults Supabase per webhook to find right host. Forward webhook (HTTP POST) to target host's local router. Each host runs identical software; only the tenant-set differs.

- **DNS-routing** (one bot per shard): conflicts with shared-bot constraint. Skip.
- **Router-proxy** (one entry router fans out): correct. Caddy + tenant-aware reverse proxy, or 30-line Node.js fanout.

### Failover / replication

10-30 tenant beta: don't replicate. Hourly backups of bind-mounted state to S3/B2. RPO 1h, RTO ~30 min ("spin up new VPS, restore, swap DNS").

100+: each tenant's state repo is already on GitHub (source of truth). VPS loss = losing in-flight sessions. Re-route webhook, `git clone`, restart containers.

### Framework-native multi-host

OpenClaw has `openclaw acp` for **agent-to-agent** routing, not **tenant** routing. Wrong abstraction for sharding. Roll your own.

---

# Q-K — First-time conversation flow

Turn-1: user just sent `/start` or first DM. No `meta.json`, no preferences, no GBrain data.

**v1 flow:**

1. Router detects first-message via Supabase (`first_seen_at IS NULL`).
2. Router *bypasses* agent for turn-1, sends fixed onboarding: "Hey, I'm Arete. What's your name and where are you based?"
3. User replies (turn-2). Router still bypasses, parses name + country, writes to `meta.json` and `identity/USER.md`, commits to state repo.
4. Turn-3+: route through OpenClaw normally. Agent picks up `USER.md` on first session start.

**Why bypass agent for turn-1/2?** Cold start is happening simultaneously. Don't charge user 30s wait + LLM call to ask their name. Hardcoded onboarding is faster, cheaper.

**Where to persist:**
- Name → `identity/USER.md`
- Country/timezone → `meta.json` (for cron) and `prefs.json` (agent-readable)
- Use-cases → `brain/onboarding.md` (GBrain ingests on first sync)

For v2, hand turn-1+ entirely to agent with `agent:bootstrap` internal hook recognizing empty USER.md.

---

# Q-M — OpenClaw hooks

Two families:

### Internal hooks (workspace-defined, command/lifecycle)

Discovered from `<workspace>/hooks/`, `~/.openclaw/hooks/`, or bundled. HOOK.md frontmatter declares events. Events: `command:new`/`reset`/`stop`, `message:received`/`preprocessed`/`sent`, `tool:before`/`after`, `session:start`/`patch`/`compact:before`/`compact:after`, `agent:bootstrap`, `gateway:startup`/`shutdown`/`pre-restart`.

### Plugin hooks (typed, in-process, can mutate/block)

Registered via `api.on("before_tool_call", …)` in plugin code:
- `before_prompt_build` — add dynamic context
- `before_agent_run` — inspect prompt, return `{ outcome: "block", message }` to short-circuit
- `before_tool_call` — gate tool execution
- `before_agent_reply` — synthetic reply, skip model
- `before_agent_finalize` — inspect natural final answer, request one more pass
- `agent_end` — observe final messages, success state, duration
- `model_call_started`/`model_call_ended` — sanitized metadata (perfect for billing)

**For Arete:**
- **First-time logic** → `agent:bootstrap` internal hook (per-tenant via syncer)
- **Humanizer** → `before_agent_finalize` plugin hook (platform-level)
- **Audit** → bundled `command-logger` + `agent_end` plugin hook
- **Cost tracking** → `model_call_ended` plugin hook → Supabase
- **Tool deny-list** → `before_tool_call` plugin hook returning `{ block: true }`

Write **one platform plugin** (humanizer + cost-tracker + tool deny-list) and bake into image. Each tenant container loads identically.

---

# Q-N — GBrain multi-tenancy

**Direct answer: GBrain is not designed for multi-tenant SaaS. Run a separate GBrain process per tenant container using PGLite. Switch to Postgres-backed only when a power user crosses 10K pages.**

README explicit: *"Multi-operator access (different users, different brains, isolation guarantees) is not the design center."* Two engine modes:

1. **PGLite** (embedded Postgres 17 via WASM, default) — good up to ~50K pages
2. **Postgres + pgvector** (Supabase or self-hosted) — shared/multi-machine

**Don't share Postgres across tenants with RLS.** GBrain's MCP server, sync, and skills assume single-tenant. Dream-cycle / enrichment would cross-contaminate brains.

### v1 architecture

```
Tenant container
├── /var/tenant/.gbrain/brain.pglite   ← per-tenant PGLite (file-based)
├── /var/tenant/state/brain/             ← per-tenant markdown (git-backed)
└── gbrain serve (MCP, stdio)            ← per-tenant MCP, bound to OpenClaw
```

Each container runs own `gbrain serve` child process; OpenClaw connects via MCP stdio. Storage: ~100-300MB/tenant in PGLite.

Env-injection wrapper script issue → cleanest fix is `entrypoint-gbrain.sh`:

```bash
#!/bin/bash
export GBRAIN_HOME=/var/tenant/.gbrain
export OPENAI_API_KEY="${EMBEDDING_API_KEY:-}"
cd /var/tenant/state
exec gbrain serve --stdio
```

### Alternatives with native multi-tenancy

- **mem0** (mem0.ai) — multi-tenant SaaS, per-user namespaces, simpler but no knowledge graph
- **Letta / MemGPT** — multi-tenant memory blocks
- **Zep** — open-source temporal knowledge graph, multi-tenant

For Arete: stick with GBrain v1 (matches Alfred reference). Re-evaluate at 50+ tenants.

---

# Q-O — Cron / scheduled jobs

OpenClaw native cron (`cron add`, `cron list`, `cron run`):
- Jobs in `~/.openclaw/cron/jobs.json`, state in `jobs-state.json`
- Timezone per-job via `--tz IANA/Name`; otherwise host TZ
- `sessionTarget`: `main`, `isolated` (default), `current`, `session:custom-id`

### Hibernation interaction

Cron fires inside gateway process. **Hibernated container = no cron.** Missed runs not replayed retroactively (overdue isolated jobs are rescheduled, not fired in a thundering herd at startup).

### v1: Router-side wake cron

Schedules in Supabase `cron_jobs(tenant_id, schedule, payload, next_fire_at)`. Router runs global scheduler:
1. Every minute: `SELECT * FROM cron_jobs WHERE next_fire_at <= now()`
2. For each due job: `docker start <container>` if hibernated, wait for gateway health, then `docker exec openclaw agent --session-key cron:<job-id> -m "<prompt>"`
3. Container re-hibernates after idle window

### v2: Per-container cron in always-warm tier

Power users on paid tier get "always warm" promise. Free tier uses router-side wake.

### Per-tenant timezones

Store TZ in Supabase `tenants.timezone`. Convert to UTC for `next_fire_at`. Container runs UTC.

### Staggering 50 tenants' 7am IST briefings

Stagger over 15-min window:
```python
fire_time = base_time + timedelta(seconds=hash(tenant_id) % 900)
```
Avg 3.3 containers waking/min, well within Docker startup capacity.

---

# Q-P — Updates / migrations

### Blue-green per container

Overkill for v1. OpenClaw is pinned in Docker image; new platform release = new image = rolling restart.

### Recommended flow

1. CI builds `arete:v2026-05-27-abc`
2. Staging test
3. Roll out: hibernated tenants get new image on next wake; warm tenants get scheduled hibernate→wake in maintenance window (2-5am tenant-local)

30 tenants in ~30 min. Acceptable.

### Hard restart all

Emergencies (CVE, broken cron). Broadcast "updating, back in 5 min", `docker stop`, deploy, restart router.

### Schema migrations (`meta.json`, `prefs.json`)

You own the schema. Write `migrations/` in platform code; entrypoint runs `node /opt/arete/migrate.js --state /var/tenant/state`. Idempotent via version field.

### Skill-name conflicts

User-skills win on name conflict. Document: "Custom skill replaces ours; you won't get updates until you delete override." At sync time, warn in-chat: "Your custom `calendar` is older than platform default. Update?"

---

# Q-Q — Observability

### Per-tenant logs

OpenClaw writes per-agent logs to `~/.openclaw/agents/<id>/logs/` and session transcripts to `<sessionId>.jsonl`. Transcripts are gold — every turn, every tool call, every response as JSONL.

```bash
docker exec arete-<tenant> openclaw sessions list --json
docker exec arete-<tenant> cat /var/tenant/.openclaw/agents/main/sessions/<sid>.jsonl | jq
```

v1: enough. v2: ship transcripts to S3-backed log lake with tenant-id tags. Don't centralize until >20 tenants.

### Cost tracking

1. **Plugin hook `model_call_ended`** — fires after every LLM call with usage metadata. Write to Supabase `usage_events` tagged with tenant_id, model, tokens, duration. Sum nightly into `tenants.month_to_date_cost`.
2. **OpenRouter/Helicone/Portkey** — proxy with built-in cost tracking. Extra hop. **v1: don't** — DeepSeek + Anthropic native is cheaper; `model_call_ended` hook suffices.

### SLA monitoring

Synthetic test tenant — monitoring sends message every 5 min via Telegram API; alerts if no reply within 30s.

---

# Q-R — Security

| Risk | Severity | Mitigation |
|---|---|---|
| Shared Docker socket → escape | 🔴 | **Don't mount `/var/run/docker.sock` into tenant containers.** Router runs on host; talks to dockerd from host. |
| Bind-mount escape via symlinks | 🟡 | `--security-opt no-new-privileges`, mount `/var/tenants/<id>` as separate FS with `nosuid,nodev,noexec` if possible |
| Container-to-container lateral | 🟡 | Per-tenant Docker network, or default bridge with `--icc=false` |
| Tenant pulls malicious skill via `openclaw skills install` | 🔴 | **Disable at runtime** via `clawhub.fetchEnabled: false`. Skills come from platform bundle + user-skills (synced from user's GitHub, they control). |
| Skill shells out to dangerous commands | 🔴 | `tools.allow`/`tools.deny` config + `before_tool_call` plugin hook to deny `bash`, `rm`, etc. by default |
| Prompt injection from inbound DM | 🔴 | Built-in DM pairing (`dmPolicy: "pairing"`) treats DMs as untrusted. Keep on. |

### Per-container limits

```bash
docker run \
  --memory=1g --memory-swap=1g --memory-reservation=400m \
  --cpus=0.5 --pids-limit=200 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --tmpfs /run:rw,noexec,nosuid,size=64m \
  --security-opt no-new-privileges --security-opt seccomp=default \
  -v /var/tenants/<id>:/var/tenant arete:latest
```

`--read-only` + tmpfs works because OpenClaw writes only inside state dir + workspace.

### Dangerous skills to deny by default

`shell_exec` (arbitrary shell), `python_repl` (code exec), `file_write` outside workspace, `network` to non-allowlisted domains (v2). Configure `tools.deny` in `openclaw.json` and enforce via `before_tool_call`.

---

# Q-S — Resource limits

```
--memory=1g --memory-swap=1g --memory-reservation=400m
--cpus=0.5 --pids-limit=200
--ulimit nofile=2048:4096 --ulimit nproc=512:1024
--log-driver=json-file --log-opt max-size=10m --log-opt max-file=3
```

**CPX32:** 10 tenants × 1GB = 10GB nominal (over-subscribed, fine if <50% active concurrently). **CPX42:** 30 tenants comfortable.

### Framework leak patterns

1. **Session transcript growth.** Set `session.maintenance.mode: "prune"`, `pruneAfter: "30d"`. Or run `openclaw sessions cleanup --all-agents` weekly.
2. **MCP loopback warm sessions.** Gateway-mode keeps them. If GBrain MCP leaks, gateway leaks. Restart containers weekly (hibernation cycle does this automatically unless "always warm").

---

# Recommendations

**Week 1 (1 tenant working):**
1. Fix `payloads[0].text` parser in router. Today.
2. Move boot-gate to CI. Delete runtime gate.
3. Replace fabricated DeepSeek pricing in business plan with $0.435/$0.87 (permanent per DeepSeek's @deepseek_ai tweet of May 22, 2026, confirmed by Engadget).
4. Verify `OPENCLAW_HOME`+`OPENCLAW_STATE_DIR`+`OPENCLAW_CONFIG_PATH` set; test nothing leaks to `/root/.openclaw`.
5. Drop circuit-breaker proxy. Use native DeepSeek provider with `fallbacks` array.

**Week 2 (10 tenants on CPX32):**
6. State-repo pool (50 pre-created repos).
7. Router-side wake cron + Supabase. Stagger 7am briefings.
8. Per-tenant `secrets.json` via file SecretRef. Test `secrets reload`.
9. Cost-tracking via `model_call_ended` plugin hook → Supabase `usage_events`.
10. Synthetic SLA monitoring tenant.

**Defer to v2:**
- Multi-host sharding (only crossing 25 tenants)
- OpenRouter/Helicone proxy layer
- Always-warm paid tier
- LLM-as-judge eval (replaces substring grader)
- Encrypted-volume per-tenant secrets

**Benchmarks that change the plan:**
- Avg tenant LLM cost > $2/mo → reconsider routing (you priced for $0.30)
- Cold-start consistently > 5s → invest in always-warm pool earlier
- GBrain > 500MB per tenant → migrate to shared Postgres+pgvector with strict per-tenant schemas
- Concurrent active tenants > 5 → upgrade to CPX42 immediately, don't wait for 30 tenants

---

# Caveats

- **OpenClaw is fast-moving.** Pin `openclaw@2026.5.22`. Don't `npm i openclaw@latest`. Diff `openclaw config schema` between versions before upgrade.
- **CLI `--json` envelope is documented for `--deliver` only.** Without `--deliver`, shape *should* be same minus `deliveryStatus`, but not contractually guaranteed. Add contract test pinning field shape, running on every image build.
- **GBrain breaking changes common.** Pin by commit SHA. v0.30-v0.38 window has documented breaking changes per the README's own footgun warnings.
- **DeepSeek geopolitical risk.** Chinese AI lab. Per Wikipedia: in March 2026 the Chinese government also moved to restrict state-affiliated use of OpenClaw itself citing security and energy concerns; concurrently several Western corporate users are restricting DeepSeek. Have Anthropic-only routing mode for tenants who request.
- **OpenClaw security advisories.** GHSA-q8ff-7ffm-m3r9 (webhook SecretRef caching) was fixed in 2026.4.x; verify your pinned 2026.5.22 is past it (it is). Subscribe to `github.com/openclaw/openclaw/security/advisories`.
- **"Single shared bot" is novel for OpenClaw.** Framework's `agents bind --bind telegram:*` assumes one operator owns Telegram. Your design routes around at Bot-API layer (you own token; OpenClaw never sees Telegram directly — router does message-in/out). Clean, but means *every* Telegram-channel feature (DM pairing, channel auth, group routing) is bypassed and reimplemented in router. Budget for that.
- **No production multi-tenant OpenClaw deployment exists publicly.** OpenClaw is overwhelmingly single-user. Closest precedent is `team400.ai` running it as managed service per client (one deployment per client, not multi-tenant). You are pioneering this pattern. Expect rough edges.