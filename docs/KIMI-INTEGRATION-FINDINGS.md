# Kimi K2.6 integration findings (2026-05-14)

Status: **RESOLVED 2026-05-15.** Root cause was `params.thinking`
missing from `openclaw.json`, not an OpenClaw upstream bug. Kimi was
running in default thinking mode → long chain-of-thought generations →
180s gateway timeout on most turns (silent fallback to gpt-5.4-mini)
and reasoning_content leaking into user-facing text on the rare turns
that did complete in time.

**Fix:** added `agents.defaults.models["moonshot/kimi-k2.6"].params.thinking
= {"type": "disabled"}` (instant mode). Codified in
`infra/configure-openclaw.sh`. Verified 2026-05-15 via Telethon E2E:
5/5 tests passed, Kimi served all 5 directly, no fallback events,
6–21s round-trip per turn.

Also fixed: fallback chain trimmed from
`[gpt-5.4-mini, sonnet-4-6, haiku-4-5]` to
`[gpt-5.4-mini, haiku-4-5]` per user's spec — sonnet is unnecessary
mid-tier given gpt-5.4-mini handles fallback well.

Historical findings below preserved for reference.

---

## Symptom

Bot replies to daily-task queries like:

> "The user wants the daily-tasks backlog again. I should re-run the
> backlog scan from the local daily tasks state and return it."

Instead of either invoking `exec ~/bin/daily-tasks.sh backlog` or
relaying the script's stdout. Pattern reproduces across every probe
that should route to a deterministic tool call.

Same prompts under `openai/gpt-5.4-mini` (the fallback in Kimi's
chain) DO invoke tool calls correctly. So the issue is Kimi-specific.

## What is NOT the cause

### 1. Kimi K2.6 cannot tool-call

Disproved by direct API test (`/tmp/kimi-test.py`):

```json
{
  "model": "kimi-k2.6",
  "tools": [{
    "type": "function",
    "function": {
      "name": "daily_tasks_show",
      "description": "Show today's task list",
      "parameters": {"type": "object", "properties": {}}
    }
  }],
  "tool_choice": "auto",
  ...
}
```

Response:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "",
      "reasoning_content": "The user wants to see today's tasks. I should call the daily_tasks_show tool and not respond with text.",
      "tool_calls": [{
        "index": 0,
        "id": "daily_tasks_show:0",
        "type": "function",
        "function": {"name": "daily_tasks_show", "arguments": "{}"}
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

Note: `content` is empty, `reasoning_content` is separate, `tool_calls`
fires correctly with `finish_reason: tool_calls`. Kimi K2.6 supports
tool calling correctly on Moonshot's openai-completions API.

### 2. Prompt engineering / SKILL.md routing

Tried, didn't help:

- Positive-framed `messages.messagePrefix` ("your turn output is one
  of: tool_call | tool stdout verbatim").
- Negative-framed "BANNED phrases" preamble (Kimi actively echoed
  the banned phrases — common LLM failure mode).
- 8 new daily-task intent classifiers in `intent-router.sh` with
  explicit EXECUTION_RECIPE per intent.
- HARD RULE block in `skills/daily-task-manager/SKILL.md`.

None of these changed Kimi's behavior because the underlying issue
is below the prompt layer — Kimi can't tool-call from inside the
OpenClaw harness even though it can from direct API calls.

## What IS the cause (best hypothesis)

OpenClaw is one of:

1. **Not sending `tools: [...]` schema** in the request to Moonshot's
   endpoint. Kimi falls back to text-generation because it has no
   declared functions to call.
2. **Concatenating `reasoning_content` into `content`** when reading
   Kimi's response — even when `tool_calls` is present. The
   "thinking" text reaches Telegram as the user-facing reply.
3. **Tool profile mismatch**: gateway config has
   `tools.profile = "messaging"` but the `exec` tool's profile list
   is `["coding"]` only. Setting `tools.exec` explicitly may
   force-enable, but it's possible the profile filter applies
   regardless.

Evidence supporting (1) and (2):

- `journalctl --user -u openclaw-gateway` shows ZERO `tool_call`,
  `toolCall`, `function_call`, or `tool_use` events around the
  failing turns. Only `sendMessage` events. Either tools aren't being
  proposed by Kimi, or OpenClaw isn't logging them, or they're
  silently dropped.
- The bot's reply text matches the SHAPE of Kimi's
  `reasoning_content` field exactly (starts with "The user wants",
  describes intended action). Direct API showed that field is
  populated and `content` is empty.

## What was tried on the live VPS

1. Moved `extensions/disabled/moonshot/` → `extensions/moonshot/`
   so OpenClaw auto-discovers the Moonshot plugin. Confirmed via
   journal: `auto-enabled plugins: moonshot/kimi-k2.6`.
2. Removed `api: "openai-completions"` overrides from
   `models.providers.moonshot` and per-model entries — letting the
   plugin's `moonshot-native` endpoint class apply.
3. Set `agents.defaults.thinkingDefault: "off"` → `"low"` (the
   moonshot plugin maps `"low"` to "on") to engage the
   `MOONSHOT_THINKING_STREAM_HOOKS` that should segregate
   `reasoning_content`.
4. Restarted `openclaw-gateway.service` after each change.

None of these changed the symptom.

## Recommended next steps

### Short-term (unblock the bot today)

Switch primary back to `openai/gpt-5.4-mini` until Kimi integration
is resolved. Leave the Moonshot extension in `extensions/` (the move
was the right thing regardless) but flip the model primary:

```bash
# in ~/.openclaw/openclaw.json
agents.defaults.models.main.primary = "openai/gpt-5.4-mini"
agents.defaults.models.main.fallbacks = [
  "moonshot/kimi-k2.6",     # try kimi second
  "anthropic/claude-sonnet-4-6",
  "anthropic/claude-haiku-4-5"
]
```

This restores known-good tool-call behavior while keeping Kimi in
the chain.

### Medium-term (proper Kimi support)

Diagnostic options the user could try without me:

1. **Instrument OpenClaw**: enable verbose request logging on the
   moonshot endpoint family. The `dist/` JS is compiled but a wrapper
   shim that logs `fetch()` calls to `https://api.moonshot.ai/v1/...`
   would reveal whether `tools` is in the request body.
2. **File an OpenClaw issue**: include the direct API test above as
   proof Kimi can tool-call, plus the live behavior as proof OpenClaw
   isn't passing tools or isn't reading the response right.
3. **Try a different Moonshot model**: `kimi-k2-thinking` and
   `kimi-k2-thinking-turbo` have `"reasoning": true` in the catalog —
   they're built for the thinking-stream-separated workflow. They
   might behave differently under the current plugin.
4. **Check if `tools.profile: "coding"` (instead of `"messaging"`)
   re-enables exec everywhere** — see if the profile gate is what
   removes exec from Kimi's tools list.

## Direct API test script

Saved at `/tmp/kimi-test.py` on the VPS. Useful reproducer for any
OpenClaw upstream issue. Demonstrates Kimi tool-calling works
correctly with a properly-formed request.

## Live state on VPS (as of 2026-05-14)

| Item | State |
|---|---|
| Moonshot extension | Moved to `/usr/lib/node_modules/openclaw/dist/extensions/moonshot/` (active) |
| `models.providers.moonshot.api` | Removed (was `openai-completions`); plugin defaults apply |
| Per-model `api` overrides for kimi-k2.6/k2.5 | Removed |
| `agents.defaults.thinkingDefault` | `"low"` |
| `agents.defaults.models.main.primary` | `moonshot/kimi-k2.6` (unchanged from start of session) |
| Fallback chain | `openai/gpt-5.4-mini → claude-sonnet-4-6 → claude-haiku-4-5` |
| Daily-task scripts (v2) | Working end-to-end via direct SSH; bot only invokes them via gpt-5.4-mini fallback |
