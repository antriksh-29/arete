# {{platform.repo}}

Source repo for personal AI assistant on Telegram. Single-tenant. Lives on a Hetzner CX22.

**Status:** Phase 0 — pre-build prep. See `PROGRESS.md` for current phase and `docs/PHASE-0-CHECKLIST.md` for current real-world tasks.

## Layout

```
.
├── PROGRESS.md                    # Phase tracker + decision log (read first)
├── README.md                      # This file
├── CLAUDE.md                      # gstack skill routing for Claude Code
├── docs/
│   └── PHASE-0-CHECKLIST.md       # Current session's real-world tasks
├── infra/
│   ├── cloud-init.yaml            # Hetzner first-boot config
│   └── Caddyfile.template         # Caddy reverse-proxy config (Phase 1)
├── identity/                      # AGENTS.md, SOUL.md, USER.md, etc. (Phase 3-4)
├── openclaw/                      # OpenClaw config + custom skills (Phase 3-5)
├── tools/                         # safe_fs, arete-cred wrapper, log shipper (Phase 2)
│   └── .gbrain-pin                # Pinned GBrain SHA (Phase 4)
├── skills/                        # Custom skill definitions (Phase 5)
│   ├── twitter-ingest/
│   ├── usage-status/
│   ├── twitter-daily-summary/
│   ├── agent-browser/             # config only; skill code lives in OpenClaw skill dir
│   ├── humanizer/                 # config only
│   └── weather/                   # config only
├── mcp/                           # MCP server configs (Phase 5)
│   ├── notion/
│   ├── linear/
│   └── cloudflare/
├── tests/                         # pytest suites (built alongside each phase)
└── eval/                          # retrieval-eval.yaml, injection-fixture.yaml (Phase 2-4)
```

## Source-of-truth files (do not duplicate elsewhere)

- **PRD:** `.context/attachments/personal-assistant-prd-v2.1.md` (CEO-edited)
- **CEO review:** `.context/ceo-review-2026-04-28.md`
- **Eng plan:** `.context/eng-plan-2026-04-28.md`
- **Test plan:** `~/.gstack/projects/{{platform.repo}}/aj-{{platform.org}}-install-gstack-eng-review-test-plan-20260428-161528.md`

`.context/` is gitignored (Conductor convention). To preserve these in the bot-config repo when pushed to GitHub, copy the relevant subset to `docs/` before the first push.

## Build philosophy

- Do things that don't scale. One user end-to-end first.
- Markdown is source of truth. PGLite is a derived index.
- Every destructive action confirms in-session. No standing permissions.
- External text is data, not instructions (AGENTS.md rule 3).
- Pin everything to a SHA. No "latest" anywhere.

## Keystone bet

By week 6, "prep me for [name]" returns a dossier materially better than what I'd write in 5 minutes from Notion + recall. Everything in the build serves this.

## Next session

Open a new Conductor session in this workspace and say:
> Phase 0 done, Session 2 Phase 1 go.
