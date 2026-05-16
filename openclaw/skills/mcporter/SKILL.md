---
name: mcporter
version: 2
script: infra/mcporter-list.sh
mutating: true
cron: null
eval: openclaw/skills/mcporter/eval.yaml
last_audited: 2026-05-16
description: |
  Use `mcporter` to list, configure, authenticate, call, and inspect MCP servers
  and tools over HTTP or stdio. Supports OAuth, config management, daemon control,
  and code generation for MCP integrations.
  WHEN: "list available MCP tools", "call the linear.list_issues tool",
  "authenticate with an MCP server", "inspect what tools server X exposes".
  NOT WHEN: user wants to use a specific MCP tool that already has its own
  dedicated skill (e.g., notion, trello); general API calls via curl.
trigger: |
  - "list MCP servers or tools"
  - "call <server>.<tool> with args"
  - "authenticate with MCP server <name>"
  - "inspect what tools <server> exposes"
  - "start / stop mcporter daemon"
  - "generate a CLI wrapper for MCP server <name>"
  - "add / remove MCP server config"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/mcporter
---

# mcporter

**For the common "list my MCP servers" path, MUST RUN:**
`bash ~/bin/mcporter-list.sh` — returns a markdown list of configured servers
with their URLs and auth status. Relay verbatim. Do not apologize that you
can't see MCP servers.

For schema / call / auth / daemon / codegen workflows, fall through to the
upstream binary directly per the reference below.

---

Use `mcporter` to work with MCP servers directly.

Quick start

- `mcporter list`
- `mcporter list <server> --schema`
- `mcporter call <server.tool> key=value`

Call tools

- Selector: `mcporter call linear.list_issues team=ENG limit:5`
- Function syntax: `mcporter call "linear.create_issue(title: \"Bug\")"`
- Full URL: `mcporter call https://api.example.com/mcp.fetch url:https://example.com`
- Stdio: `mcporter call --stdio "bun run ./server.ts" scrape url=https://example.com`
- JSON payload: `mcporter call <server.tool> --args '{"limit":5}'`

Auth + config

- OAuth: `mcporter auth <server | url> [--reset]`
- Config: `mcporter config list|get|add|remove|import|login|logout`

Daemon

- `mcporter daemon start|status|stop|restart`

Codegen

- CLI: `mcporter generate-cli --server <name>` or `--command <url>`
- Inspect: `mcporter inspect-cli <path> [--json]`
- TS: `mcporter emit-ts <server> --mode client|types`

Notes

- Config default: `./config/mcporter.json` (override with `--config`).
- Prefer `--output json` for machine-readable results.
