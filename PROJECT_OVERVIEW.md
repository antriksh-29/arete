# Personal Assistant — Project Overview

A technical download of how the two repos behind **Alfred** (a single-tenant personal AI assistant on Telegram) work together.

- **Runtime / orchestration**: [`antriksh-29/personal-assistant`](https://github.com/antriksh-29/personal-assistant)
- **Knowledge base (the "brain")**: [`antriksh-29/antriksh-brain`](https://github.com/antriksh-29/antriksh-brain)

> Anti-scaling philosophy: built end-to-end for one user (Antriksh). The success bar is "a Telegram prompt like *prep me for [name]* returns a dossier materially better than 5 minutes of manual Notion + recall."

---

## 1. System architecture at a glance

```
 ┌─────────────┐   Telegram   ┌────────────────────────────┐
 │   User      │ ───────────► │  @alfred_antriksh_bot      │
 │ (Antriksh)  │ ◄─────────── │  (OpenClaw daemon on VPS)  │
 └─────────────┘              └──────────────┬─────────────┘
                                             │
                ┌────────────────────────────┼──────────────────────────────┐
                ▼                            ▼                              ▼
        ┌──────────────┐           ┌────────────────┐              ┌────────────────┐
        │ Skills       │           │ Tools          │              │ Connectors     │
        │ (37 routed)  │           │ browser-harness│              │ Composio MCP   │
        │ brain-ask,   │           │ creds vault,   │              │ (Gmail,Cal,    │
        │ ingest-pdf,  │           │ safe_fs, log   │              │  Notion, X)    │
        │ agent-browser│           │ shipper        │              │                │
        └──────┬───────┘           └────────┬───────┘              └────────┬───────┘
               │                            │                               │
               └────────────────┬───────────┴───────────────────────────────┘
                                ▼
                ┌──────────────────────────────┐        ┌────────────────────────┐
                │  GBrain (v0.22.14)           │ ─────► │  antriksh-brain repo   │
                │  pgvector + Supabase Postgres│        │  (markdown KB,         │
                │  RRF + entity boost +        │ ◄───── │   auto-committed)      │
                │  recency decay               │        └────────────────────────┘
                └──────────────────────────────┘
```

The **personal-assistant** repo holds the agent runtime, skills, infra, and safety scaffolding. The **antriksh-brain** repo is the durable, human-readable knowledge graph (markdown + wikilinks) that GBrain writes into.

---

## 2. Repo 1 — `personal-assistant` (the runtime)

### 2.1 Stack

| Layer | Choice | Notes |
|------|--------|-------|
| Bot runtime | **OpenClaw** (Node.js + Anthropic SDK) | Telegram polling; custom skill injection |
| Primary LLM | **Claude Sonnet 4.6** | |
| Fallback LLM | **Kimi K2.6** (thinking disabled) | A/B routes only |
| Knowledge base | **GBrain v0.22.14** | pgvector + Supabase Postgres |
| Browser tool | **browser-harness** (CDP @ 127.0.0.1:9222) | Persistent user-data dir; Composio Browser is fallback |
| Connectors | **Composio MCP** | Gmail, Google Calendar, Notion, X/Twitter |
| Host | **Hetzner CX22** (Falkenstein, EU) | Caddy reverse proxy, 2GB swap |
| Orchestration | **systemd user services** | `bh-chrome.service`, `brain-collectors.timer`, heartbeat timers |
| Secrets | Encrypted SQLite vault (`tools/creds.py`) + 1Password master key | CLI: `bot-cred get/set/list/audit` |
| Observability | Axiom log shipping (`tools/log_shipper.py`) | Tails `/var/log/openclaw/*.log`, redacts creds, structured JSON |
| CI/CD | GitHub Actions `deploy.yml` → SSH → `infra/deploy-vps.sh` | Pre-push hook gates pushes |

### 2.2 Directory layout

| Path | Role |
|------|------|
| `identity/` | Agent constitution. `AGENTS.md` (14 rules), `SOUL.md` (voice), `USER.md` (context), `TOOLS.md` (capabilities), `ACCESS_POLICY.md` (4-tier data sensitivity). Mix of auto-gen + hand-written. |
| `openclaw/` | OpenClaw config, custom skills source, injection-fixture smoke test, retrieval-eval baseline. Deployed to `~/.openclaw/workspace/` on the VPS. |
| `skills/` | **37 skill definitions** routed by intent (`RESOLVER.md`). Each has `SKILL.md` + `eval.yaml`. Examples below. |
| `infra/` | 107 files: bash/Python/Node deploy & ops scripts, systemd units, Caddyfile, cred vault CLI, log shipper, brain collectors. Entry: `infra/deploy-vps.sh`. |
| `tools/` | Local tools beside OpenClaw: `safe_fs.py` (path enforcement), `creds.py` (vault), `smoke_test.py` (injection fixture runner), `.gbrain-pin` (pinned GBrain SHA). |
| `eval/` | `retrieval-eval.yaml` (10 labeled queries, nDCG@3 baseline), `injection-fixture.yaml` (30 patterns, 100% refusal required at boot), pytest suites. |
| `docs/` | ADRs (e.g. ADR-001 frozen 2026-05-18: browser-harness primary), phase checklists, e2e findings, Kimi integration notes. |
| `tests/` | pytest e2e suites for brain_ask, layer3, reading_list, browser bake-off. |
| `.github/workflows/` | CI/CD definitions. |
| `CLAUDE.md` | Project instructions for Claude Code (gstack skills, browse rules). |

### 2.3 Skills (37 total — selected)

- **Retrieval / reasoning**: `brain-ask`, `brain-find`, `brain-compare`
- **Ingestion**: `ingest-pdf`, `ingest-youtube`, `ingest-x-thread`, `ingest-article`
- **Action**: `agent-browser` (browse, click, fill forms via the persistent Chrome), `google-composio` (Gmail + Calendar via MCP), `twitter-composio`
- **Productivity**: `daily-task-manager`, `reading-list`
- **Style/safety**: `humanizer` (output voice blending with `/corpus/` samples), `identity-audit` (safety self-check)

### 2.4 Critical conventions / gotchas

- **browser-harness invocation is stdin/heredoc only.** The `-c` flag silently fails (fixed in `c5a7a43`).
- **ADR-001 (2026-05-18)** froze browser-harness as primary after a bake-off: BH median **34.5s** vs Composio **35.2s**, BH slightly richer output, $0 vs MCP quota cost. **Fallback triggers after 3 consecutive BH failures.**
- **YAML list parser must be quote-aware** — regexes containing commas were being split (fixed in `a17c892`).
- **Injection smoke test runs at systemd `ExecStartPre`** and blocks daemon startup on *any* refusal miss across the 30-pattern fixture.
- **Per-request confirmation** required for write ops (ClawVisor approval flow).
- **4-tier `ACCESS_POLICY.md`** — sensitive data (passwords, secrets) never reach model output.

### 2.5 Entry points

- **User-facing**: Telegram `@alfred_antriksh_bot` (polling).
- **Deploy**: `git push main` → GH Actions → SSH → `infra/deploy-vps.sh` → systemd restart.
- **Local CLIs**: `bot-cred`, `brain-ask`, `brain-find`, `brain-compare`, `gbrain skillpack install`.
- **Scheduled tasks**:
  - `brain-collectors.timer` — daily 7am IST Gmail + Calendar fetch.
  - Heartbeat timers — morning briefing 8am, meeting-prep at 15-min intervals.

### 2.6 Current state (last ~15 commits)

Focus: **browser-harness maturity & OpenClaw integration**.

| Commit | What |
|--------|------|
| `64b92cf` | `twitter-composio` skill + AGENTS.md routing + known-bug doc |
| `86e4375` | Add twitter to curated Composio toolkit list |
| `012cf3a` | Wire the write-as-you-learn loop in agent-browser (editable learning surface) |
| `564457b` | Freeze ADR-001 — browser-harness primary, Composio fallback |
| `a17c892` | Quote-aware YAML list parser (was splitting regexes on inner commas) |
| `ca2fc05` | E2E direct openclaw-agent runner (29/30 pass on browser bake-off) |
| `c5a7a43` | Fix heredoc `-c` bug; surface learning loop |
| `38aa910` | E2E Composio MCP coverage |
| `b46faae` | Retire clawvisor — use Composio MCP for Gmail+Calendar |
| `5d0ed55` | Composio MCP server setup wrapper |

**Phases 0–7** (bare metal, safety, agent, brain, skills, connectors, heartbeat) mostly complete. **Active blocker**: Phase 7 write capabilities awaiting Gmail/Calendar OAuth re-grant with expanded scopes. **Phases 8–9** (model A/B soak, steady state) intentionally not started.

---

## 3. Repo 2 — `antriksh-brain` (the knowledge base)

> *"Personal AI assistant brain, auto-managed by OpenClaw + GBrain."*

The brain is a **markdown-based, semi-automated knowledge graph** with git as the transaction log. GBrain ingests sources, extracts entities, creates/updates files, and auto-commits.

### 3.1 Directory layout (~301 files)

| Path | Count | Role |
|------|-------|------|
| `/companies/` | 53 | Company entities — mostly auto-stubs |
| `/people/`    | 53 | Person entities — mostly auto-stubs |
| `/concepts/`  | 125 | Ideas / patterns / domains (ai-agents, founder-mode, agent-memory…) — the core knowledge layer |
| `/sources/`   | 5 | Ingested articles / PDFs with full summaries |
| `/corpus/`    | 6 | Curated voice samples (naval, garrytan, antriksh) — fed to `humanizer` skill |
| `/habits/`    | 7 | Daily trackers (book-reading, daily-tasks, water-intake, activity) |
| `/media/`     | — | Social / YouTube ingested sources |
| `/meetings/`, `/notes/`, `/projects/`, `/lists/` | — | User-authored content |
| `*.md` (root) | 12 | Redirects / indexes (e.g. `ai-agents.md` → `concepts/ai-agents.md`) |

### 3.2 File schema

All files use **YAML front-matter + markdown body** with inline **wikilinks** (`[[concepts/ai-agents]]`).

Auto-generated stub:
```yaml
---
type: company   # or: person, concept, habit, list, project, x_corpus
created: 2026-05-03
updated: 2026-05-03
---
# Company Name
_Auto-created stub from [source-type] ingestion on [date]._
First referenced by [[sources/slug]]. Fill in details on next mention.
```

Rich, hand-written / GBrain-synthesized file (e.g. `sources/qoves-com-...md`, 12KB):
- Front-matter: `type`, `title`, `aliases`, `tags`, `name`, `created`, `updated`, `tweet_count`
- Sections: **Why this matters**, **The argument**, **Connections** (wikilinks), **One-line takeaway**, **Stats** (habits)
- Per-fact source tags: `[Source: User, brain seed, 2026-04-30]`

### 3.3 Special files

| File | Purpose |
|------|---------|
| `.xref-processed.jsonl` | Cross-reference audit log. Each line: `{slug, entities, source_label, queued_at, processed_at, linked_ok, linked_failed}`. |
| `.xref-queue.jsonl` | Pending ingestions (currently empty). |
| `.last-bookmark.json` | Last-read article metadata (title, URL, slug, completion ts). |
| `.reading-list-v2-migrated*` | Migration flags — reading-list was migrated 3 times. |
| `/projects/personal-ai-assistant.md` | Master project doc, names the assistant and locks the stack (OpenClaw 2026.4.26, GBrain v0.22.14, Supabase + pgvector, Hetzner, Telegram, 29 skills, AGENTS.md). |
| `/lists/to-read.md` | Bookmark queue (9 entries), managed by `~/bin/reading-list.py`. |

### 3.4 Retrieval algorithm (GBrain)

Hybrid ranking pipeline:
1. Vector recall via pgvector embeddings over the markdown corpus.
2. **RRF (Reciprocal Rank Fusion)** to merge with lexical/entity recall.
3. **Entity boost** when query mentions a known wikilink target.
4. **Recency decay** to favor fresher content.

(No weighted-sum tuning until eval data forces an upgrade — held back deliberately.)

### 3.5 Ingestion flow

```
OpenClaw skill (ingest-pdf / ingest-article / ingest-x-thread / ingest-youtube)
        │
        ▼
download to /home/botuser/.openclaw/media/inbound/<slug>
        │
        ▼
GBrain entity extraction (LLM)
        │
        ▼
create/update stubs in /companies/, /people/, /concepts/
add full summary in /sources/<slug>.md
resolve wikilinks; record results in .xref-processed.jsonl
        │
        ▼
git commit (e.g. "habit: book-reading update 2026-05-19 10:02")
git push origin main
```

The brain is **read-only from the runtime's perspective at query time** — all writes are GBrain-driven, no PR flow.

### 3.6 Known issues / smells

- **Case-sensitivity collisions**: both `companies/Anthropic.md` and `companies/anthropic.md` exist (likewise `YC.md`/`yc.md`/`ycombinator.md`). The xref log shows `linked_failed: ["companies/Anthropic", "concepts/AI-agents"]` while parallel lowercase entries succeed — slug-normalization bug or migration artifact.
- **Stub proliferation**: 125 concepts but only ~10–15 are rich; same for companies/people (53 each, mostly bare stubs). Brain bootstrap was lower-volume than projected (~30 pages day-1 vs 80–170 projected).
- **Orphaned references**: `concepts/CLAUDE-md`, `companies/Mem0`, `companies/Zep`, `concepts/AI-agents` show as `linked_failed` even though files exist — slug ↔ filename mismatch.

### 3.7 Naming conventions

| Entity type | Convention | Example |
|-------------|-----------|---------|
| Concept files | lowercase-hyphenated | `concepts/ai-agents.md` |
| People / companies | CamelCase | `people/Brian-Chesky.md`, `companies/Anthropic.md` |
| Corpus | `x-<handle>.md` | `corpus/x-naval.md` |
| Dates | ISO 8601 | `2026-05-19` |
| Root redirects | bare slug | `ai-agents.md` → `concepts/ai-agents.md` |

---

## 4. How the two repos interact

| Direction | Mechanism |
|-----------|-----------|
| `personal-assistant` → `antriksh-brain` | GBrain runs inside the OpenClaw process on the VPS. After each ingestion batch it `git commit && git push` to `antriksh-brain` main. |
| `antriksh-brain` → `personal-assistant` | The brain is mounted/cloned on the VPS; pgvector indexes the markdown files; `brain-ask` / `brain-find` skills query through GBrain at request time. |
| Audit trail | `.xref-processed.jsonl` in the brain + git history + Axiom logs in the runtime give end-to-end traceability of any fact. |
| Safety | `identity/ACCESS_POLICY.md` (in runtime) gates what brain content can be surfaced; `tools/safe_fs.py` enforces path boundaries on writes back to the brain. |

---

## 5. Safety & evals (cross-cutting)

- **Constitution**: `identity/AGENTS.md` — 14 rules the agent must obey.
- **Injection fixture**: `eval/injection-fixture.yaml` — 30 prompt-injection patterns; **100% refusal required** at daemon `ExecStartPre`. A miss blocks startup.
- **Retrieval eval**: `eval/retrieval-eval.yaml` — 10 labeled queries with nDCG@3 baseline; regressions surface in CI.
- **Access tiers**: 4-tier data sensitivity in `ACCESS_POLICY.md` — passwords/secrets never reach model output.
- **Write confirmation**: All write-side ops (send email, create event, post tweet) require per-request user confirmation in Telegram before execution.
- **Credential vault**: `bot-cred` CLI wraps an encrypted SQLite vault; master key in 1Password; never in env vars.

---

## 6. Brainstorm prompts (suggestions when sharing this doc)

Some natural directions the architecture invites:

- **Brain hygiene**: fix slug normalization and merge case-collision files; promote stubs into rich notes via a scheduled "stub harvester" skill that uses recent retrieval misses as targets.
- **Proactive layer**: today the heartbeat does morning briefing + meeting-prep — extend to weekly review, reading-list nudges, "you said you'd follow up with X" reminders pulled from `/meetings/`.
- **Write-as-you-learn loop** (`012cf3a`) just landed for `agent-browser` — generalize the editable learning surface to other skills (e.g. `google-composio` learns common email patterns).
- **Eval-driven retrieval tuning**: with `retrieval-eval.yaml` in place, unlock weighted-sum tuning that was deliberately deferred.
- **Multi-channel input**: brain ingests via OpenClaw skills today — Telegram-only entry. Voice (Whisper) or email-forwarding could be cheap new mouths.
- **Cross-entity reasoning**: brain has wikilinks but `brain-compare` is a single skill — explore a "graph walk" skill that finds non-obvious paths between two entities for prep dossiers.

---

*Generated 2026-05-19 from a snapshot of both repos. Verify against latest `git log` before quoting versions or commit SHAs.*
