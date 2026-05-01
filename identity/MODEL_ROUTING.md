# Model routing decisions

Garry-style routing document, adapted to our setup. Records which model
runs which task and why. Update on any change.

Garry's pattern (from gbrain `skills/conventions/model-routing.md`) uses
Opus 4.6 as the default and Sonnet as the cheap-tier — because his cost
discipline is calibrated to Opus prices. We're set up with Sonnet 4.6 as
the default; the equivalent tier-down for us is Haiku 4.5 (or, for very
narrow classification work, GPT-5.4-nano).

---

## Current routing (active)

| Task | Model | Why |
|---|---|---|
| Telegram main turn (every conversation reply) | **Haiku 4.5** (Sonnet 4.6 fallback) | Switched 2026-05-01. Re-verified same evening after a misdiagnosis (see lesson below). Haiku is reliable when AGENTS.md is precise. |
| Daily morning briefing (8am IST cron) | **Haiku 4.5** (inherited from main) | Same path. ~3× cheaper than Sonnet for the daily briefing. |
| Meeting prep (every 15min poll, fires when meeting in 25-40min window) | **Haiku 4.5** | Same. |

### Lesson: I misdiagnosed Haiku once. Capturing it so we don't repeat.

On 2026-05-01 21:00 IST, a real Telegram failure happened: user asked
"do you have any idea about my hydration activity for today?" and the
bot replied "no hydration logs recorded" while 2L was logged. My first
instinct was to blame Haiku ("narrating without acting") and revert to
Sonnet. **That was wrong.** When I dug into the trajectory file (the
`.trajectory.jsonl` artifact for that turn — read it from
`~/.openclaw/agents/main/sessions/<uuid>.trajectory.jsonl`), I saw:

- Haiku DID emit two tool calls
- Tool 1: `exec ~/bin/habit-log.sh readback water` → exit 127 (file not
  found). Agent guessed the wrong script name based on the skill folder
  name "habits"; the actual script is `~/bin/water-log.sh`.
- Tool 2: `gbrain__search "hydration water today 2026-05-01"` → empty.
  Agent picked `__search` (keyword tsvector) instead of `__query`
  (hybrid vector + keyword + RRF). The brain page is found by `__query`
  at 0.84; not found by `__search` on those tokens.
- Both tools failed → agent honestly concluded "no logs."

The root cause was **AGENTS.md ambiguity**, not the model:
1. The "Capabilities" line said "READ skills/habits/SKILL.md" — too soft.
   Agent skipped reading and guessed a script name.
2. Brain capability didn't disambiguate `gbrain__query` (hybrid, content
   lookup) vs `gbrain__search` (keyword, exact match). Agent picked the
   weaker tool.

Both fixed: inlined exact script paths + tool-selection disambiguation
in AGENTS.md. After fix, 4 tests on the Telegram channel path with
Haiku → 4 successes (hydration, brain query, multi-tool briefing,
reminder), 0 failures.

**General lesson:** when an agent fails, READ the trajectory file before
attributing the failure to a model. The trajectory shows EXACTLY what
the model emitted — text blocks, tool calls, tool results. If the model
emitted tool calls but they failed/empty, the bug is in your tool
definitions / scripts / skill instructions, not the model. If the model
emitted no tool calls at all, then question the model — but only after
ruling out documentation gaps.

Trajectory file path:
`~/.openclaw/agents/main/sessions/<sessionId>.trajectory.jsonl`. Find
the session ID in `sessions.json` under the appropriate session key.
| Web search (`~/bin/web-search.sh`) | **GPT-5.4-mini** | Search + synthesis are both well in mini's ability. Per-call cost ~$0.003 vs ~$0.05 if Sonnet did it. |
| Bootstrap enrichment (one-time, Phase 4.5) | **Sonnet 4.6** | One-shot foundational generation; structural quality of seed pages matters. Done. |
| ClawVisor's intent verifier | **claude-haiku-4-5** | ClawVisor's internal choice (their service, their model). Out of our control. |
| gbrain embeddings (every page write) | **OpenAI text-embedding-3-large** | Hardcoded in gbrain. Cost ~$0.0001 per page. Don't change. |
| Reading check-in compose (`~/bin/reading-checkin.sh ping`) | None — pure shell + Telegram | No LLM call. Stats are computed by the script. |
| Habit logging | None — deterministic shell | Same. |

## Models registered in `agents.defaults.models`

- `anthropic/claude-sonnet-4-6` (primary)
- `anthropic/claude-opus-4-7` (registered as quality fallback, not currently routed to)
- `openai/gpt-5.4-nano` (registered as alias `gpt-nano`, not currently routed to)

---

## Why nano is rejected for signal-detector

When implementing the full ambient signal-detector (currently deferred),
the right model is **Haiku 4.5**, not nano. Reasoning:

- Signal-detector writes structured brain pages with strict schema
  (frontmatter, sections, citations, entity-link `[[type:slug]]` syntax).
- Compass-artifact research (`.context/attachments/...md`) explicitly warned
  that weaker models degrade on multi-step skill-following — exactly the
  failure mode here.
- Haiku 4.5 is positioned by Anthropic for fast classification + extraction
  + structured generation — the sweet spot for this workload.
- Cost difference per 100 messages/day: Haiku ~$16/mo vs nano ~$1/mo.
  $15/mo is cheap insurance against malformed pages corrupting the brain.
- Nano is correct for simple binary classification (was-this-a-question?,
  route-to-A-or-B?) — much narrower than signal-detector's job.

---

## Routing decisions deliberately deferred

These are real opportunities but not worth building today.

### Habit-shaped trivial replies on Sonnet

When user types "750ml" or "yes" in response to a check-in, the main agent
fires Sonnet to (a) recognize the intent, (b) call `exec` with the right
log script, (c) reply with confirmation. Could route to Haiku 4.5 with a
small intent-classifier in front. Saves ~$0.04/day. Not worth it yet.

### Full ambient signal-detector

Custom OpenClaw plugin listening for `message_received`, spawning a parallel
Haiku call per inbound message. Captures everything, even when the main
agent is busy with another tool. Build when:
- Inline capture in main turn proves to be missing important signal, or
- Volume crosses ~200 turns/day where the marginal cost is worth the
  ~$0.50/day investment.

### Cheap-tier intent classifier

Nano-tier classifier in front of every Telegram message that decides
"trivial / standard / complex" and routes the main turn accordingly:
- Trivial → Haiku 4.5
- Standard → Sonnet 4.6 (current default)
- Complex → Opus 4.7 (currently unused)

Worthwhile when daily Sonnet spend exceeds ~$5/day. Currently ~$2-3/day.

### Per-skill model override

OpenClaw supports `model:` in skill frontmatter to route specific skills to
different tiers (per Garry's pattern). Currently unused. Add when a specific
skill proves consistently over- or under-tiered.

---

## Cost control levers (in order of impact)

These are the actions that materially reduce cost without sacrificing
quality. Apply in this order; each is reversible.

1. **Trim AGENTS.md** — every char in AGENTS.md is paid for every turn at
   Sonnet rates. Done 2026-05-01: 21K → 14K. Ongoing: don't let it grow
   without trimming. Keep tool detail in skills, not AGENTS.md.

2. **Filter MCP tool surface** — every tool definition is paid per turn.
   Tried 2026-05-01 (gbrain 41 → 11 via `infra/gbrain-mcp-filter.mjs`),
   **reverted same day** at user's call. Reasoning:
   - Savings at Haiku rates were only ~$3-4/mo (vs ~$15/mo at Sonnet rates).
   - Operational friction of "agent says it needs tool X, edit filter, scp,
     restart" outweighs the savings.
   - Full toolset means new gbrain capabilities are available immediately
     when use cases emerge, no maintenance step needed.

   The filter file (`infra/gbrain-mcp-filter.mjs`) is preserved in the repo
   for reference. If we ever revert to Sonnet primary or volume goes 5×,
   re-evaluate (math: at Sonnet + 250 turns/day, savings would be ~$50/mo
   which probably crosses the friction threshold).

3. **Honest assessment when adding new automations** — if the automation
   doesn't need Sonnet judgment (pure templating, fixed transformations),
   keep it as deterministic shell. Don't add an LLM call where shell math
   suffices (the reading-checkin.sh stats are pure Python — no LLM).

4. **Tier-down explicit tasks where Haiku/mini suffice** — case-by-case
   when adding a new skill. Frontmatter `model:` field per Garry's
   convention.

5. **Last resort, hard cap** — `agents.defaults.dailySpendCap` or similar.
   Only set if the above don't keep cost in line.

---

## Anti-patterns (cost mistakes to avoid)

- **Don't put detail in AGENTS.md** — every character recurs every turn.
  Skills are loaded on-demand; detail belongs there.
- **Don't expose MCP tools you don't use** — they cost prompt tokens with
  zero benefit. Filter with the wrapper pattern.
- **Don't use Sonnet for templating** — if the task is "format X as Y with
  no judgment," use shell. The reading check-in compose is a worked example.
- **Don't downgrade primary turns to save pennies** — the difference between
  Sonnet and Haiku on a complex turn is the difference between right and
  wrong, not "slightly worse." Premature optimization.

---

## How to update this doc

Any time you:
- Change a model assignment for a task
- Add a new MCP server that exposes tools to the agent
- Add a new automation that involves an LLM call
- Hit a cost surprise

…update this file with the change and reasoning. It's the record of where
the cost is going.
