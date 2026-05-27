# MCP-first design principle

> *"Contract-first: `src/core/operations.ts` defines ~41 shared operations… CLI and MCP server are both generated from this single source."*
> — gbrain `CLAUDE.md`, the architectural spec by Garry Tan

This is the architectural rule for this project. Read it before adding any
new external integration.

---

## The rule

**For any external service the agent might need to talk to during a
conversation turn, build an MCP server first. Backend scripts come second,
only for batch / cron work that doesn't need to be agent-reachable in
real time.**

In Garry's gbrain design, every capability is defined as an *operation* in
a single contract, and that contract auto-generates both the CLI surface
*and* the MCP server. There's no "this is a backend-only capability" —
if a capability exists, it's MCP-exposed by default, with trust
filtering applied at the MCP layer (`OperationContext.remote = true`
tightens filesystem confinement, etc.).

## The three surfaces

| Surface | What it is | When to use |
|---|---|---|
| **MCP tools** | Typed, structured I/O. Agent's primary live interface. Auth scoped to the tool. Reliability primitives (retries, backoff, two-phase commits) baked into the wrapper. | **Default for every external service the agent might need.** Live calendar, live Gmail, live web search, external APIs — all MCP. |
| **Skills (markdown)** | Workflow orchestration in fat markdown files. Tool-agnostic. Compose existing MCP tools, add domain logic, enforce quality bars. | Always — but skills don't *create* tool surfaces, they *use* them. |
| **Backend scripts (cron / oneshot)** | Privileged or scheduled work the agent shouldn't be in the loop for | Bulk ingestion, daily syncs, periodic maintenance, one-off utilities. Anything not needed during a conversation turn. |

The clearest way to think about it: **if the agent might want this data or
action during a Telegram conversation, it goes in MCP. If it's something
that runs on a clock and writes to the brain, it can be a backend script.**

## Why MCP-first matters

We learned this the hard way. The gaps that bash + exec-allowlist patterns
introduce, that disappear when you go MCP:

1. **Environment isolation is solved.** OpenClaw strips env when spawning
   MCP children for security. With a wrapper script that pulls auth from
   the arete-cred vault and exports it before exec, the MCP server has
   exactly what it needs — and nothing else. Bash + exec patterns either
   leak env or have to re-pull from vault on every call.

2. **Structured I/O eliminates parsing fragility.** MCP tools return
   JSON-schema-typed responses. Bash scripts return free-text — every
   parse on the agent side is a chance for drift (e.g. calendar `start`
   field changing from object `{dateTime}` to flat ISO string between
   API versions; we hit this).

3. **Trust boundary at the right layer.** Garry's `OperationContext.remote`
   triggers tighter filesystem confinement and rate-leasing for MCP
   callers. Bash + exec gives the agent *all* shell, with no per-tool
   policy.

4. **Reliability primitives are co-located with the tool.** The MCP server
   handles retries, backoff, session management, error normalization. The
   agent doesn't need to remember any of this. Each call is clean.

5. **Composability.** Once `calendar_list_events` is an MCP tool, any
   skill or any agent can call it without re-implementing the curl boilerplate.

## What we built that isn't yet MCP (and why)

Some integrations were bash-script-first. Each is a candidate to migrate:

| Surface | Current form | Should it be MCP? |
|---|---|---|
| **gbrain** | MCP ✅ | already done |
| **Google Calendar + Gmail** (via Composio MCP since 2026-05-16) | MCP ✅ | already done — Composio gateway replaced the prior ClawVisor wrapper, brings 500+ apps under one consumer key |
| **web search** (`~/bin/web-search.sh`) | bash + exec allowlist | **yes, but low priority** — single-call surface, output is already markdown which is the desired return shape. Migrate when we want stricter contracts on cost-tier routing or when adding alternate providers. |
| **send-telegram** (`~/bin/send-telegram.sh`) | bash + exec | **no** — single-purpose, fire-and-forget, no parsing. Bash is fine. |
| **schedule-reminder** (`~/bin/schedule-reminder.sh`) | bash + exec | **no** — same reason. systemd-run is the actual primitive; the script is a thin wrapper. |
| **reading-checkin** (`~/bin/reading-checkin.sh`) | bash + exec | **maybe later** — habits in general (reading, workout, meditation) could be one MCP server with `habit_log`, `habit_stats`, `habit_send_checkin` tools. Migrate if/when we add a 3rd or 4th habit. |

The principle isn't "everything must be MCP." It's "anything the agent
needs to interact with **structurally** during a conversation must be MCP."
Single-shot bash wrappers for fire-and-forget operations are fine.

## The audit checklist (when adding any new integration)

Before merging, ask:

1. Does the agent need this data **during a conversation** (not just via
   ingested brain pages)? → **MCP required.**
2. Is the input or output structured (more than a single text field)? → **MCP preferred.**
3. Does it need retries, rate-limiting, or auth refresh? → **MCP** (those primitives live in the MCP server cleanly).
4. Is it a privileged operation (shell, deletion, mutation outside the
   workspace)? → **Backend script with explicit gating** (per gbrain's
   `GBRAIN_ALLOW_SHELL_JOBS=1` pattern).
5. Is it batch/cron work the agent doesn't need to know about? →
   **Backend script** is fine.

If you answer yes to (1), (2), or (3), the answer is MCP.

## Reliability patterns to copy from gbrain

Garry's `gbrain/CLAUDE.md` documents these explicitly. Adopt them in any
new MCP server we build:

- **Exponential backoff with adaptive load awareness** —
  `gbrain/src/core/backoff.ts` checks CPU/memory and active-hours multiplier
  before retrying.
- **Rate-lease concurrency caps** — `gbrain/src/core/minions/rate-leases.ts`
  has a default key (`anthropic:messages`) with `max via
  GBRAIN_ANTHROPIC_MAX_INFLIGHT` and `renewLeaseWithBackoff` retrying 3x
  (250/500/1000ms). Apply this for any paid API.
- **Two-phase tool persistence** — `pending → complete/failed` with
  replay reconciliation for mid-dispatch crashes. OpenClaw already
  has this; the MCP server doesn't need to replicate it.
- **Deterministic-first, LLM-fallback** — `gbrain/src/core/fail-improve.ts`
  logs every LLM fallback to JSONL so patterns can be promoted to
  deterministic over time. Use this for any classifier-style logic.

## When adding a new MCP server, follow this template

1. **Folder:** `infra/<name>-mcp/` with `server.mjs` + `package.json` +
   `wrapper.sh`.
2. **Server code:** Node + `@modelcontextprotocol/sdk`, stdio transport,
   typed tool schemas.
3. **Auth:** read from `process.env`, with `infra/<name>-mcp/wrapper.sh`
   pulling from the arete-cred vault and exporting before `exec node ...`.
4. **Tool design:** one tool per discrete capability. Fresh session/UUID
   per call where the upstream API needs it. Coherent reason/instruction
   strings tested against any intent verifiers.
5. **Errors:** return `{isError: true, content: [{type: 'text', text: ...}]}`
   so the agent sees the actual cause rather than a swallowed exception.
6. **Register:** `mcp.servers.<name>.command` → `~/bin/<name>-mcp-wrapper.sh`
   in `~/.openclaw/openclaw.json`.
7. **Verify:** `openclaw agent --agent main --json -m "list your tools"`
   should show `<name>__*` tools. Then exercise each tool through a real
   agent turn.

## Summary

MCP-first, contract-first. Skills compose tools; tools wrap services;
backend scripts handle the rest. When in doubt, look at how gbrain does it
and copy the pattern.
