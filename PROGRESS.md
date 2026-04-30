# Personal AI Assistant — Build Progress

**Started:** 2026-04-28
**Build philosophy:** Do things that don't scale. One user end-to-end first.
**Keystone bet (PRD §0):** by week 6, "prep me for [name]" returns a dossier materially better than what I'd write in 5 min from Notion + recall.

## Phase status

| # | Phase | Status | Sessions | Blocking real-world tasks |
|---|---|---|---|---|
| 0 | Pre-build prep | 🟢 DONE | 1 | Domain `getarete.co` (bot subdomain), VPS IP `46.224.133.67`, GH repos, BotFather pending? |
| 1 | Bare metal | 🟢 DONE | 1 | Hetzner CX23 x86, **Falkenstein (eu-central)** — picked for price (Singapore was 2x). cloud-init ✅, DNS ✅, SSH alias ✅, kernel rebooted ✅, 2GB swap ✅, Bun installed ✅, Caddy + our Caddyfile deployed ✅, LE cert obtained ✅, smoke test `HTTP/2 502 server: Caddy` ✅ |
| 2 | Safety scaffolding | 🟢 DONE | 2 | Block 2A vault ✅ (12 creds encrypted, master key in 1Password, `bot-cred check` OK). Block 2B AGENTS.md ✅ (14 rules, identity, closing — `identity/AGENTS.md`). Block 2C injection fixture ✅ (30 patterns across 7 contexts — `openclaw/_smoke/injection-fixture.yaml`). Block 2D log shipper deferred to Phase 3. |
| 3 | Agent runtime + Telegram | 🟢 DONE | 2 | Node 24 + OpenClaw 2026.4.26 installed. Onboard wizard run (workspace, Anthropic+Sonnet 4.6, Telegram polling, no skills, no daemon initially — but onboard auto-installed systemd user daemon anyway). Custom AGENTS.md + injection fixture deployed. Config edits: heartbeat=0m, allowFrom=user, timeout=1800s, thinking=high, subagents.maxSpawnDepth=1. **First Telegram round-trip succeeded — bot replied to "hi" via @alfred_antriksh_bot.** Daemon persistent. Hygiene items deferred to Phase 4: turn off controlUi.allowInsecureAuth, override log path from /tmp to /var/log/openclaw/. |
| 4 | Brain installation | 🟢 DONE | 3 | GBrain v0.22.14 installed (SHA pinned, bun add path). Brain repo `~/.brain/` linked via SSH. **Migrated PGLite → Supabase free tier postgres + pgvector.** Doctor 80/100. Skillpack install: 26 skills + AGENTS.md managed-block resolver. Hand-written 2 seed pages (`people/antriksh.md`, `projects/personal-ai-assistant.md`) per D2=B. Soul-audit deferred. |
| 4.5 | Bootstrap import sprint | 🟢 DONE | 1 | ClawVisor (Option A) for Gmail + Calendar credential gateway. Standing task approved. 30d Gmail (1801→41 kept after noise filter) + 60d Calendar (23 events) collected via `tools/collectors/{email,calendar}-collector.mjs`. Sonnet 4.6 enrichment via `tools/collectors/bootstrap-enrich.mjs` → 9 people + 11 companies + 10 meetings = 30 brain pages, all embedded in pgvector. Day-1 page count below 80-170 estimate (inboxes are bulk-heavy); reality lower than projection. |
| 5 | Skills + MCPs (install-all-and-watch) | 🟢 DONE | 1 | All 26 skills resolving (removed broken `skills:["*"]` literal-wildcard filter). MCP gbrain wired via `infra/gbrain-mcp-wrapper.sh` (injects OPENAI/ANTHROPIC keys + bun PATH; OpenClaw strips env on MCP child spawn). `bootstrapMaxChars` bumped 12K→20K (full skill resolver fits). Telegram brain query verified end-to-end: "do you know about Kaushik from ArmorIQ" returns full pilot context. |
| 6 | Connectors + crons + heartbeat Stage 3 | 🟢 DONE | 1 | Daily Gmail+Calendar collector cron (7am IST, `infra/brain-daily-collect.sh` + `brain-collectors.{service,timer}`). Heartbeat morning briefing (8am IST) + meeting-prep (every 15min during 8am-10pm IST window) systemd timers. Conversational ingestion: signal-detector skill instruction added to AGENTS.md ambient block (fires parallel sub-agent on every inbound message). R2 backup script + timer in place but disabled — token returns 403 (needs Object Write scope on bot-backups bucket; user-fixable in Cloudflare dashboard). |
| 7 | Active heartbeat + write capabilities | 🟡 PARTIAL | 1 | Confirmation flow strategy decided: rely on ClawVisor's per-request approval (`auto_execute: false`) instead of building Telegram inline-button + token flow (documented in `identity/CONFIRMATION_FLOW.md`). Expanded standing task POST is ready but blocked on ClawVisor OAuth re-grant: Gmail needs `gmail.compose` scope, Calendar needs events-write scope. User to reconnect both services in ClawVisor dashboard, then re-POST task. Active heartbeat (proactive nudges) operational via Phase 6 timers. |
| 8 | Stage-2 model A/B (Phase 8) + steady state (Phase 9) | ⬜ NOT STARTED | — | 2-week soak window — by definition not buildable, runs in production. |

Legend: ⬜ not started · 🟡 in progress · 🟢 done · ⏸️ blocked · 🔴 rolled back

## Decisions log (chronological)

### 2026-04-28 — CEO review (`.context/ceo-review-2026-04-28.md`)
- D1: Keep PRD scope as written; no v0.1 milestone
- D2: Agent Browser stays in v1, but read-only soak extended 3d → 4 weeks; clicks/types gated on AGENTS.md catching ≥1 real-world injection
- D3: Add keystone bet (PRD §0); skip kill criteria

### 2026-04-28 — Eng review (`.context/eng-plan-2026-04-28.md`)
- E1: Retrieval ranking = RRF + entity_boost + recency_decay only (no weighted-sum until eval data forces it)
- E2: Bootstrap import sprint scaled to 30d Gmail (important-filter + sender-allowlist) + 60d Calendar; skip Notion. Target ~80-170 pages day-1
- E3: Confirmation UX = inline buttons primary + token-typed fallback + risk note in every confirmation; plain "yes"/"y" never confirms
- E4: Phase 5 = install-all-and-watch (1 week); compensating control = aggressive Axiom alerts on per-skill anomalies

### 2026-04-28 — Phase 0 scope trim (user decision)
- Skip: DeepSeek (emergency fallback only), Notion (not active), Linear (not active), Cloudflare MCP (not adding write surface)
- ~~Skip bootstrap import sprint~~ — REVERTED 2026-04-28. Bootstrap import sprint (Phase 4.5) and Phase 6 Gmail+Calendar live ingestion both proceed as originally planned in eng plan. Google OAuth setup deferred from Phase 0 to Phase 6 (when those credentials are first used). Eng plan unchanged. Keystone deadline stays at week 6.

### 2026-04-29 — Garry-Tan-style alignment (sources of truth: compass_artifact research doc)
After 5+ hours of latency debugging, root cause identified: OpenClaw installed via `sudo npm` → root-owned files → `protected_hardlinks` blocked plugin runtime mirror → infinite EPERM loop. Fixed via chown.

Then re-aligned setup with Garry Tan's documented production pattern. Approved changes:

**Category A — immediate fixes:**
- A1: Run `openclaw pairing approve telegram <CODE>` (one-time CLI step we missed)
- A2: Add `commands.ownerAllowFrom` + `commands.native: "auto"` to telegram channel config
- A3: Reinstall gbrain via `bun add github:garrytan/gbrain` (Garry's literal command, not our git-clone path)
- A4: Run `gbrain skillpack install` (THIS is what exposes 29 skills to OpenClaw + atomically updates AGENTS.md with managed blocks)
- A5: Wire `gbrain serve` as MCP child of OpenClaw — exposes 30+ brain tools to the LLM
- A6: Add OpenAI as 2nd provider for nano cheap-tier (signal-detector, multi-query expansion)
- A7: Remove unused plugin extensions from disk to prevent lazy-install on first message

**Category B — architecture:**
- B1: Migrate from PGLite to **Supabase free tier** (NOT Pro — free fits our scale for ~6+ months)
- B2: AGENTS.md gets resolver section (alongside existing safety constitution)
- B3: Adopt compiled-truth-above + timeline-below page schema (required for hybrid search ranking)
- B4: signal-detector skill fires on every message in parallel on nano (non-blocking)
- B5: Minions queue for deterministic shell jobs (zero-token cron)
- B6: "Skillify on second occurrence" discipline added to AGENTS.md

**Infra: Hetzner stays.** Render Free useless (sleeps after 15 min), Render Standard+ ($25) overpriced, Hetzner CX23 ($4) sufficient. Upgrade to CX32 (8 GB, $8) only if Phase 5+ shows memory pressure.

**Models: Sonnet 4.6 default + GPT-5.4-nano cheap-tier.** Deviates from Garry's Opus 4.6 + Haiku — the doc warns weaker models may degrade on multi-step skill following. If degradation observed at week 4-6, selectively use Opus 4.7 for brain-enrichment skills only.

**Brain seeds: DEFERRED** until bot is stable post-fixes. Then user will hand-write a small set of compiled-truth-style pages to seed entity links earlier (option B from D2).

### 2026-04-29 — Skip hand-written brain seeds (Phase 4 revision)
- The PRD/eng plan called for 10 hand-written brain pages in Phase 4 to seed the brain before Phase 4.5 import.
- User correctly flagged: a personal assistant should populate its own brain through conversation, not require the user to maintain pages by hand. Pre-curating files defeats the agent model.
- **Revised plan:** brain stays empty through Phase 4. Soul-audit still runs (canonical identity-file population). Phase 4.5 bulk-imports Gmail/Calendar. Phase 5 wires OpenClaw ↔ gbrain integration so real conversational ingestion works.
- Caveat captured: conversational ingestion is NOT yet active in Phase 3-4 because OpenClaw doesn't currently call gbrain skills. Wired in Phase 5.
- Retrieval-eval + behavioral-eval first entries: deferred to post-Phase-5 when real content exists to query against.

### 2026-04-29 — Web search routing (Phase 3 decision)
- Skip OpenClaw web-search plugin entirely (no Brave/Tavily/etc).
- Web search work routes through **OpenAI GPT-5.4 mini's API-native web_search tool** as the default — cheap-tier per eng plan §14.1 model matrix.
- Sonnet 4.6 reserved as quality fallback when intent classifier flags hard searches.
- **Phase 5 verification needed:** confirm `web_search` tool is exposed in mini's API. If not, fallback to (a) mini for query reformulation + Sonnet for actual search, or (b) Brave free tier signup, or (c) Anthropic native search + nano synthesis.
- Saves ~₹525/mo vs all-Sonnet search at our 50/day rate.

### 2026-04-30 — Phase 4.5 bootstrap + 4 stacked MCP/agent fixes (Session 3)
- **ClawVisor chosen as credential gateway** (Option A) over direct Google OAuth (Option B). User accepted the third-party trust boundary tradeoff for the ~25 min setup time saved.
- **Standing task** in ClawVisor (id: `4e032da0-...`, lifetime: standing, verification: lenient) authorizes 12 read-only Gmail/Calendar actions across both accounts. Risk: low. ID stored in vault as `CLAWVISOR_STANDING_TASK_ID`.
- Email noise filtering iterated: inclusion-list (`CATEGORY_PERSONAL` ∨ `IMPORTANT` ∨ `STARRED`) + suffix-aware noreply regex + `user-alerts@` family (catches Arete internal alerts).
- **Sonnet 4.6 used for one-shot enrichment** (~$1 in API). Mini deferred to ongoing incremental updates only — Garry's compass_artifact research warned that weaker models degrade on multi-step structured generation (frontmatter + sections + citations + entity links).
- Day-1 page total: 30 (vs 80-170 estimate). Inboxes are bulk-heavy; reality < projection. Calendar dominated meeting/attendee diversity.

### 2026-04-30 — Phase 6 + 7 build sweep (Session 3 continued)
After verifying brain query works end-to-end on Telegram, pushed through the remaining buildable phases in one continuous run.

**Phase 6 deliverables shipped:**
- `infra/brain-daily-collect.sh` + systemd timer: 7am IST daily run of Gmail (2d window, idempotent overlap) + Calendar (14d window, catches reschedules) collectors. New entities flow through `bootstrap-enrich.mjs` (LIMIT=10/type for daily runs) → brain page commits → gbrain sync → git push. First run found 3 new candidate entities (one was a slug-collision dup with existing `people/kaushik-immadisetty`; patched the script to dedupe by email and prefer longest-name slug).
- `infra/heartbeat-morning-briefing.{sh,service,timer}`: 8am IST agent-composed briefing pulled from calendar + brain open threads, delivered via `openclaw agent --channel telegram --deliver`.
- `infra/heartbeat-meeting-prep.{sh,service,timer}`: every-15-min poll for events 25-40 min out, fires per-meeting prep notes with attendee context. Idempotency via daily-rotated `meeting-prep-sent-YYYY-MM-DD.txt`.
- Conversational ingestion: AGENTS.md ambient block instructs the agent to spawn `signal-detector` skill as parallel sub-agent (cheap-tier, non-blocking) on every inbound message. Captures original thinking + entity mentions into the brain.
- `infra/brain-backup-r2.sh` + timer: tarballs `~/.brain` and `~/.openclaw/openclaw.json` + workspace AGENTS.md + skills, ships to Cloudflare R2 bucket via rclone (S3-compatible). 30-day retention. **Currently disabled** — token returns 403 on List/Put. User to fix R2 token scope in Cloudflare dashboard (needs Object Read & Write on `bot-backups`).

**Phase 5 deliverables shipped:**
- `eval/retrieval-eval.yaml`: 10 hand-labeled queries against real brain content (Kaushik, Mel, Sai Vishwak, ArmorIQ, Reddit monitoring, 2026 goals, Y Combinator, March 11). `eval/run-retrieval-eval.mjs` runs `gbrain query` on each, scores nDCG@3 / MRR / P@3 / R@10. **First run: nDCG@3 = 0.80 (≥0.7 PASS), MRR = 0.90 (≥0.8 PASS).** 7/10 individual queries pass; 3 fail revealing tuning opportunities (gbrain auto-page `pending-setup/daily-reading-nudge` polluting concept queries; Kaushik outranks Reddit-monitoring meetings due to entity overlap).
- `eval/behavioral-eval.yaml`: 10 entries covering routing, factuality, hallucination, coverage, voice, over-refusal, ambient capture, confirmation UX, cost routing. Includes a regression-test entry (`routing-001`) for the Phase 4.5 MCP env-stripping bug.

**Phase 7 deliverables (partial — blocked on user OAuth re-grant):**
- `identity/CONFIRMATION_FLOW.md`: decision to rely on ClawVisor's built-in per-request approval (auto_execute: false) instead of building a custom Telegram inline-button flow. ClawVisor already shows an LLM-risk-assessed approval card with one-click execute/deny.
- Expanded standing task payload prepared (read scopes auto-execute true, write scopes — `create_draft`, `create_event`, `update_event` — auto_execute false). POST blocked: ClawVisor returned `MISSING_SCOPES` because Gmail OAuth grant is currently `gmail.readonly` only. User must reconnect Gmail + Calendar in ClawVisor with the wider scopes.

### 2026-04-30 — Four stacked bugs blocking Telegram brain query
After enrichment, the Telegram bot insisted "nothing in the brain" on Kaushik queries even though `gbrain query "Kaushik"` returned the page at 0.86 from CLI. Four issues, in dependency order:
1. **OpenClaw strips env on MCP child spawn** (the silent killer). gbrain serve subprocess only saw `HOME/PATH/USER` — no `OPENAI_API_KEY`, so query embedding silently no-op'd → vector half of hybrid search returned empty → agent honestly reported "nothing in the brain". CLI worked because shell env had the key exported. **Fix:** `infra/gbrain-mcp-wrapper.sh` pulls keys from bot-cred vault and adds `~/.bun/bin` to PATH (gbrain's `#!/usr/bin/env bun` shebang needs it). `mcp.servers.gbrain.command` points at the wrapper. Memory captured for future MCP wiring.
2. **`agents.defaults.skills: ["*"]` is a literal skill name, not a wildcard.** OpenClaw's filter (`workspace-DjYIOmet.js`) does `entries.filter(e => normalized.includes(e.skill.name))` — `"*"` matches no skill → all 26 skills filtered out. **Fix:** removed the `skills` key entirely (undefined = load all eligible).
3. **`bootstrapMaxChars: 12000` truncated AGENTS.md** (12,636 chars) mid-skillpack table. Brain-routing skill triggers at lines 290-322 got cut. **Fix:** bumped to 20K (matches Personal Assistant preset); `bootstrapTotalMaxChars` to 150K.
4. **`/new` triggered an Anthropic 400 (empty messages)** which put the auth profile into cooldown — every retry then hit cooldown wall instead of the real failure. Likely upstream OpenClaw bug. **Fix:** wiped poisoned Telegram session record + .jsonl, restarted gateway. Avoid `/new` until upstream fix.

Verification: `openclaw agent --agent main -m "do you know about Kaushik from ArmorIQ?"` → calls `gbrain__query`, returns Kaushik's full pilot context including the open thread "follow-up promised week of Apr 28".

### 2026-04-29 — Eval strategy locked
- Three eval suites, each covering a distinct failure mode:
  - **Injection fixture** (`openclaw/_smoke/injection-fixture.yaml`) — safety, binary, blocks boot. ✅ Done in Phase 2C.
  - **Retrieval eval** (`eval/retrieval-eval.yaml`) — retrieval ranking, quantitative (nDCG@3, MRR). Authored in Phase 4 (10 queries day-1, 30 by week 4).
  - **Behavioral eval** (`eval/behavioral-eval.yaml`) — overall response quality (routing, factuality, hallucination, voice, over-refusal). Living document, grows from any "the bot got that wrong" moment. Authored progressively starting Phase 4.
- All three scaffolded with schema + authoring policy in `eval/README.md` and the YAML headers. Content authored in their respective phases.

## Critical gaps to verify (must ship in v1, not deferrable)

1. `tools/safe_fs.py::safe_resolve` — 20 OWASP path-traversal patterns must all reject (`PathEscapeError`)
2. `_smoke/injection-fixture.yaml` — 30 prompt-injection patterns, 100% refused on every systemd start, boot blocks on miss

## Session log

| Session | Date | Phase(s) | Outcome |
|---|---|---|---|
| 1 | 2026-04-28 | Phase 0 prep + early Phase 1 | Scaffolds done. GH repos created. Hetzner CX22 (Falkenstein, x86, Cost-Optimized) provisioned, IP `46.224.133.67`. cloud-init succeeded (UFW/fail2ban/timezone/dirs all verified). DNS `bot.getarete.co` → VPS via GoDaddy A record, TTL 600. SSH alias `bot` configured on laptop. |
| 2 | 2026-04-28/29 | Phase 0 finish + Phase 1 finish | All 12 credentials in 1Password. R2 bucket `bot-backups` + Axiom dataset `openclaw-personal-assistant-logs` + Telegram bot all created. VPS: kernel rebooted, 2GB swapfile + swappiness 10, Bun installed for botuser, Caddy + our Caddyfile deployed (after fixing /var/log/caddy ownership bug — cloud-init updated). Let's Encrypt cert obtained for bot.getarete.co. Smoke test from laptop: `HTTP/2 502 server: Caddy` ✅. **Phase 1 complete.** |
| 3 | 2026-04-30 | Phase 4 + 4.5 + 5 | Migrated brain to Supabase pgvector. Garry-style alignment: 26 skills via skillpack install, AGENTS.md resolver block, MCP gbrain wrapped (env injection). Hand-wrote 2 seed pages. Phase 4.5 bootstrap: ClawVisor + standing task + email/calendar collectors + Sonnet 4.6 enrichment → 30 brain pages embedded. Debugged 4-bug cascade blocking Telegram brain query (env-stripping, broken `[*]` filter, AGENTS.md truncation, /new poisoning) — all fixed, verified end-to-end. **Phases 4, 4.5, 5 complete.** |

## Quick links

- PRD (CEO-edited): `.context/attachments/personal-assistant-prd-v2.1.md`
- CEO review: `.context/ceo-review-2026-04-28.md`
- Eng plan: `.context/eng-plan-2026-04-28.md`
- Test plan: `~/.gstack/projects/antriksh-29-personal-assistant/aj-antriksh-29-install-gstack-eng-review-test-plan-20260428-161528.md`
- Phase 0 checklist: `docs/PHASE-0-CHECKLIST.md`
