# ADR-001 — Browser stack: browser-harness primary, Composio Browser fallback

**Status:** Accepted, frozen 2026-05-18
**Owner:** the user
**Supersedes:** none

## Decision

For every browser task originated by the Telegram bot:

1. **browser-harness** is the primary tool. The agent invokes it via the
   `agent-browser` skill (`skills/agent-browser/SKILL.md`) using
   heredoc / stdin form against the persistent headless Chrome at
   `bh-chrome.service` on CDP 127.0.0.1:9222.
2. **Composio Browser** (`COMPOSIO_BROWSER_TOOL_*` MCP tools) is the
   fallback. The agent reaches for it only after 3 consecutive
   browser-harness failures using the recipes in
   `skills/agent-browser/SKILL.md` (timeouts, empty body, CDP errors
   the interaction-skills/ docs don't resolve).
3. No third browser engine. browser-use cloud profile-sync, raw
   Playwright, the OpenClaw built-in browser plugin, etc. — all off.
   If browser-harness misses a case, fix browser-harness (extend
   `agent-workspace/agent_helpers.py`) before adding another tool.

## Why (data-backed)

Side-by-side production run on real Telegram, 5 representative tasks
each, 2026-05-18 (full logs in `.context/browser-bakeoff/` and
`/tmp/tg-bh5.json`, `/tmp/tg-cm5.json`):

| Task | browser-harness | Composio Browser |
|---|---|---|
| Top-3 HN stories | ✅ 150.5s · titles + sources + comment counts | ✅ 46.5s · titles only |
| docs.openclaw.ai title + heading | ✅ 34.5s · "Documentation Index" | ✅ 28.6s · "What is OpenClaw?" |
| CoinMarketCap BTC price | ✅ 57.9s · **$76,804.15 USD, down 2.08% in 24h** | ✅ 35.2s · **$76,698.07** |
| openclaw.ai/blog latest post | ✅ 29.0s · title + date + read time | ✅ 33.1s · title only |
| browser-harness GitHub stars | ✅ 30.8s · **13.1k stars** | ✅ 37.4s · **13,102** stars |

Median latency on real Telegram: **BH 34.5s · Composio 35.2s** — tied.
Aggregate accuracy: BH 5/5 real-data correct · Composio 5/5 real-data
correct. BH replies are routinely richer (sources, dates, percentages,
counts). Composio replies are terser but consistently correct.

Direct-call benchmark (CLI, no Telegram pipeline, 15 tasks each, same
day) showed BH ~2.5× faster on median (16.8s vs 42.4s). Composio
scales worse on hard pages (openclaw blog 140s, screenshot 179s).

## Why NOT Composio primary

- Slower on rapid-iteration tests (cloud round-trip per CREATE_TASK
  + WATCH_TASK + GET_OUTPUT_FILE).
- Composio's MCP layer can produce stale "initiated" connection states
  when speculatively probed (a discovered bug as of 2026-05-16; see
  the composio fix commit).
- Composio's hosted browser pool means the bot's reliability becomes a
  Composio uptime dependency. browser-harness runs on the VPS we own.
- Cost: BH = $0 marginal per call (already-paid VPS Chrome). Composio
  counts against the MCP quota.

## Why NOT browser-harness 100% (the fallback exists)

- Hosted browser pool has a different rendering profile and may
  succeed on edge cases where the local stack stalls.
- Async task model is useful for "go do this multi-step thing while I
  work on other stuff" patterns the local sync model doesn't fit.
- Diverse rendering = real-world resilience.

## Operational guarantees enforced by this ADR

1. `BH_DOMAIN_SKILLS=1` is set in `openclaw-gateway.service` via a
   systemd drop-in installed by `infra/deploy-vps.sh`. Auto-surfaces
   per-host playbooks from `~/.browser-harness/repo/domain-skills/`.
2. The `bh-chrome.service` systemd unit is always-on, persistent
   user-data dir.
3. `~/.browser-harness/repo/` is an editable git clone — edits to
   `agent-workspace/agent_helpers.py` and `domain-skills/<site>/` persist
   and compound across runs.
4. Channel-session bloat is bounded: `tg-session-hygiene.timer` runs
   every Sunday at 04:00 IST and resets the Telegram session if its
   `.jsonl` exceeds 200 KB. Stops the bloat-then-cascade pattern that
   killed the 2026-05-18 test runs.
5. The `infra/reset-tg-session.sh` script gives a one-command manual
   reset if cascade timeouts happen between hygiene runs.

## When to revisit this decision

- Composio's median latency drops below BH's on the standard 5-task
  set for two consecutive weeks of measurement.
- A specific high-frequency use case (e.g. authenticated multi-step
  flow we use daily) consistently fails on BH and works on Composio.
- browser-harness loses upstream maintenance (no commits in 6+ months).
- A meaningfully better third browser stack appears (e.g. Browser Use
  Cloud's native MCP, if pricing makes sense).

Re-running the bake-off is the standard re-evaluation: 5 tasks ×
2 browsers via `infra/e2e-direct.py` and `infra/e2e-harness.py`,
side-by-side, fresh Telegram session.

## Related artifacts

- `skills/agent-browser/SKILL.md` — invocation form, learning surface,
  fallback rule.
- `identity/AGENTS.md` "Browser intents" routing table.
- `infra/e2e-harness.py` — Telethon production-grade tests.
- `infra/e2e-direct.py` — CLI-direct iteration tests.
- `infra/reset-tg-session.sh` — manual session reset.
- `infra/tg-session-hygiene.{service,timer}` — automated weekly reset.
- `~/.browser-harness/repo/` — editable browser-harness clone on VPS.
- `~/.context/browser-bakeoff/REPORT.md` (local, gitignored) —
  original 10-test bake-off that motivated this decision.
