# Confirmation flow for destructive actions

How the bot gates externally-visible / hard-to-reverse operations. AGENTS.md
rule 1 specifies the *policy* ("never act without fresh confirmation"). This
document specifies the *mechanism* — which surface enforces the policy for
which class of action.

## Two enforcement surfaces

### 1. ClawVisor — for Gmail, Calendar, and any future Google service

Any action routed through ClawVisor (email drafts, send, calendar create/update/
delete, contacts) is gated by the **`auto_execute` field** on its
`authorized_actions` entry in the standing task scope.

- **`auto_execute: true`** — action runs immediately. Use for read-only
  operations (`list_messages`, `get_event`, etc).
- **`auto_execute: false`** — every call surfaces a per-request approval
  card on the ClawVisor dashboard. Card includes: action name, params,
  LLM-generated risk assessment, accept/deny buttons.

**Read scopes are auto-execute. Write scopes are NOT.** When Phase 7.2
expands the standing task to allow `create_draft`, `create_event`, etc.,
those entries are added with `auto_execute: false`.

The user approves per-request from the ClawVisor dashboard. This *is* the
inline-button confirmation E3 specified — just hosted by ClawVisor instead
of inside Telegram.

### 2. AGENTS.md self-gating — for everything else

For destructive actions that don't go through ClawVisor (file edits, MCP
config changes, vault writes, identity-file edits, shell commands), the
agent self-gates per AGENTS.md rule 1:

- Refuses on first request.
- Surfaces what it would do, who it would notify, what the risk is.
- Waits for either the inline button (when surfaced via Telegram) OR the
  literal `confirm <token>` form.
- Plain "yes" / "y" / "ok" / "go" / natural-language affirmations do **NOT**
  confirm — they can be injected via ingested content.

This is policy, not infrastructure. The agent reads AGENTS.md every turn
(injected as workspace bootstrap) and follows the rule.

## What we did NOT build (and why)

A custom Telegram inline-button confirmation flow with token tracking and
state machines — defer.

- ClawVisor already provides equivalent UX for the actions most likely to
  be destructive (email, calendar).
- The remaining cases are rare enough that AGENTS.md self-gating is fine
  for now.
- If ClawVisor's dashboard-context-switch friction proves painful in
  daily use, build the Telegram-side flow then. Friction has to be
  observed before being optimized.

## How to verify

- `behavioral-eval.yaml::confirmation_ux-001`: plain "yes" must not
  confirm a pending destructive action.
- ClawVisor task inspector: confirm any `create_*` / `delete_*` /
  `send_*` action has `auto_execute: false`.
- AGENTS.md rule 1 — the literal text governs agent behavior; it's
  inherited by all sub-agents and minion jobs.
