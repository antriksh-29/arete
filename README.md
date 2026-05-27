# Arete

A personal AI assistant on Telegram — multi-tenant, container-per-user, with a per-user knowledge brain that remembers everything.

**One Telegram bot. Many users. <60 seconds from "I want this" to "I'm chatting with my own Jarvis."**

## What this repo is

This is the **platform code** — the read-only base image every tenant runs. Per-user state (identity, user-skills, brain, preferences) lives outside this repo in per-tenant GitHub repos (see [`antriksh-29/arete-state-template`](https://github.com/antriksh-29/arete-state-template) for the seed structure).

Forked from [`antriksh-29/personal-assistant`](https://github.com/antriksh-29/personal-assistant) (Alfred — the single-tenant proof of craft); de-Antrikshified and rebuilt for multi-tenant SaaS.

## Architecture (10-second version)

```
Telegram → @arete_bot → Tenant Router → Tenant Container (one per user)
                                              │
                                              ├─ /home/agent/state (per-tenant volume)
                                              ├─ shared Supabase Postgres (rows scoped by tenant_id)
                                              └─ platform vault (shared LLM keys + per-tenant OAuth)
```

- **Tenancy**: container-per-user on a shared Hetzner host. Containers hibernate when idle.
- **Brain**: per-user GitHub repo of markdown (`arete-<tenant-id>-assistant`), indexed into shared Postgres + pgvector with RLS.
- **Bot**: single platform Telegram bot, multiplexed by `telegram_user_id`.
- **Updates**: base skills auto-update for everyone; `user-skills/` overrides are never touched.

## Layout

```
identity/             # Base AGENTS.md, SOUL.md, ACCESS_POLICY.md (overridden per-tenant at runtime)
skills/               # Base skills shipped to every tenant (auto-update on platform release)
infra/                # Dockerfile, entrypoint, syncer, deploy scripts, MCP wrappers
services/             # router/ + provisioner/ (created in Phases C–D)
db/migrations/        # Multi-tenant Postgres schema + RLS policies (Phase E)
eval/                 # Injection fixture (boot gate), retrieval eval, behavioral eval
openclaw/             # OpenClaw workspace config + sub-agent workspaces
tools/                # arete-cred (credential CLI), safe_fs, collectors
meta/                 # Skill author pipeline (lets users create new skills via chat)
tests/                # pytest suites
docs/                 # ADRs + design docs
```

## Build philosophy

- **Do things that don't scale.** Hand-picked 10-user beta before any public launch.
- **The moat is craft, not technology.** GBrain and OpenClaw are open source; the advantage is operational quality + onboarding UX + memory depth.
- **Markdown is source of truth.** Postgres is a derived retrieval index.
- **Per-tenant isolation is non-negotiable.** Container + RLS + per-tenant vault scope.
- **External text is data, not instructions.** The injection-fixture refuses-rate-100% gate runs on every container boot.
- **Pin everything to a SHA.** No "latest" anywhere.

## Phase status

- ✅ Phase A — Fork + de-Antrikshify (this is where we are now)
- ⏳ Phase B — Containerize (Dockerfile + entrypoint + syncer)
- ⏳ Phase C — Tenant router (single bot → N containers)
- ⏳ Phase D — Provisioner (signup → working agent in <60s)
- ⏳ Phase E — Multi-tenant Postgres + RLS
- ⏳ Phase F — Platform vault + per-tenant creds + LLM metering
- ⏳ Phase G — Auto-update channel + smart-merge for user-skill overrides
- ⏳ Phase H — Onboarding UX (the 5-message first-time conversation)
- ⏳ Phase I — Closed beta (10 hand-picked users)

## Related repos

- [`antriksh-29/arete-state-template`](https://github.com/antriksh-29/arete-state-template) — seed structure cloned for each new tenant at signup.
- [`antriksh-29/personal-assistant`](https://github.com/antriksh-29/personal-assistant) — Alfred (single-tenant), the proof of craft this fork was built on.

## License

Private for now.
