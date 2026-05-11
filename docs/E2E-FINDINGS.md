# E2E harness findings — 2026-05-10

> **STATUS (2026-05-11): step B complete — all three bugs resolved.**
>
> - **Bug #1 (silent-hang):** fixed in PR #24 by lowering
>   `agents.defaults.bootstrapTotalMaxChars` from 200000 → 60000 via the
>   new `infra/configure-openclaw.sh` (idempotent, wired into
>   `deploy-vps.sh`). Per-turn context dropped ~70%, keeping the bot
>   safely under the gpt-5.4-mini 200K TPM ceiling.
> - **Bug #2 (status-ping leak):** fixed in two passes —
>   `is_status_ping()` filter (PR #24) plus a `reply.text` snapshot at
>   capture time (PR #29) so subsequent in-place edits by OpenClaw
>   can't poison the assertion.
> - **Bug #3 (flaky weather-today):** resolved as a side-effect of #1.
>
> Validation evidence: 5× consecutive step-A 3-test runs + a full
> 14-test suite landed 14/14 effective (one transient self-throttle on
> weather-today passed on a solo retry 30s later). Zero silent-hangs
> across 30+ turns. The original investigation below is preserved for
> reference; do **not** re-investigate without first re-confirming a
> regression with a fresh harness run.

First three real-bot runs of `infra/e2e-harness.py` surfaced three
distinct issues. The harness machinery itself works (Telethon driver,
YAML parser, assertion engine, summarizer, session refresh, friendly
re-auth on revoke). The bugs are in the **bot runtime and in the
harness's reply-detection logic**. None block daily bot use — but
they make automated regression testing unreliable until fixed.

This doc is the entry point for **step B** of the validation work:
each bug below is a self-contained investigation with a hypothesis,
an evidence pointer, and a probable fix surface.

---

## Bug #1 — Bot silently hangs after rate-limit cascade (severity: HIGH)

### Symptom

During the first full-suite run (13 tests with 2-second inter-test
gaps), only 2 of 13 messages produced replies. The bot's
`openclaw-gateway` process stayed `active` per `systemctl` but
processed nothing for ~30 minutes. Manual restart unblocked it.

### Evidence

- `journalctl --user -u openclaw-gateway --since "30 min ago"`: only 2
  `sendMessage ok` lines plus 2 `embedded run agent end` events for
  13 incoming messages.
- Earlier rate-limit observation (working session before the hang):

  ```
  [agent/embedded] embedded run agent end: ... isError=true model=gpt-5.4-mini
    error=⚠️ Rate limit reached for gpt-5.4-mini ... TPM Limit 200000, Used 177203, Requested 41345
  ```
  Repeated 6+ times in succession.
- Bot's per-turn context cost: **~41K tokens** (744-line AGENTS.md +
  49 SKILL.md files injected by OpenClaw). 200K TPM = ~5 turns/min
  ceiling.

### Hypothesis

OpenClaw's retry budget on rate-limit errors is too generous — keeps
retrying gpt-5.4-mini instead of failing over to the configured Claude
fallback. After enough retries, something gets into a deadlock state
and the gateway stops processing new messages.

### Fix surfaces (in order of expected impact)

1. **Reduce per-turn token cost** — biggest lever. 41K is fat. Possible cuts:
   - Trim AGENTS.md further (currently 744 lines, was 1057). Target ~400.
   - Filter SKILL.md injection by intent — don't send all 49 every turn.
   - Aggressive prompt caching so repeated turns reuse the same cached prefix.
2. **Cap retry budget at rate-limit** — patch OpenClaw to fail clean after 3 rate-limit retries on gpt-5.4-mini, immediately fall over to claude-haiku-4-5 or claude-sonnet-4-6.
3. **Add a max-turn timeout** — if a turn takes > 120s, give up, send the user an apology message, unblock the queue.

### Reproducing for step B

Send 5-10 messages to `@alfred_antriksh_bot` over ~30 seconds. Watch
`journalctl --user -u openclaw-gateway -f` in another terminal.
Look for the cascade.

---

## Bug #2 — Status-ping leaks into reply detection (severity: MEDIUM)

### Symptom

`usage-today` test's reply was captured as `📊 session_status` (16
chars). That's OpenClaw's turn-status indicator emoji, not a real
answer. The harness treated it as the bot's reply and ran assertions
against it.

### Evidence

- `/tmp/e2e-step-a2.json` `usage-today.reply_text` = `"📊 session_status"`
- Test elapsed: 6.2s (way too fast for a real usage-status reply,
  which involves a CLI call + LLM-summarize).
- OpenClaw emits these status pings as outbound messages from the bot
  to indicate turn progress. They look like real bot messages over
  the wire.

### Hypothesis

OpenClaw's status-ping messages match our filter `sender_id != sent.sender_id`
just like real replies do. We grab the first one. Real reply comes
later but our test has already moved on.

### Fix surfaces

In `infra/e2e-harness.py` → `run_test`:

```python
# Filter out OpenClaw status pings: short, emoji-prefixed, contain
# known status sentinels. Keep waiting for a non-status reply.
STATUS_SENTINELS = ["📊 session_status", "⌛", "🤔", "thinking..."]

def is_status_ping(text: str) -> bool:
    t = (text or "").strip()
    return any(t.startswith(s) for s in STATUS_SENTINELS) or len(t) < 5

# In the poll loop, ignore status pings; only return on a "real" reply.
bot_replies = [m for m in msgs
                if m.sender_id != sent.sender_id
                and not is_status_ping(m.text)]
```

Plus add an option per-test: `wait_for_final: true` to wait until 5
seconds pass without a new message (settle period), then take the
last message. That handles bots that send progressive replies.

---

## Bug #3 — Flaky weather-today (severity: LOW — symptom of #1)

### Symptom

Same test (`weather-today`, same input "weather today") passed in
11.1s on one run, timed out at 60s on the next. Identical message,
identical bot state at start.

### Hypothesis

This is a downstream symptom of bug #1. When the bot's rate-limit
headroom is low (recent activity exhausted TPM budget), `weather-today`
hits the cascade and never replies. When the bot is fresh, it replies
cleanly.

### Fix

No separate fix needed — addressing bug #1 should eliminate the
flakiness here automatically. Keep this as a regression marker:
**after fixing bug #1, weather-today should pass reliably across
10 consecutive runs.**

---

## What worked (don't regress these)

- **Telethon session lifecycle** — `setup-telethon-session.py`
  generates a session string the harness reads via the gitignored
  `~/.telethon-alfred-e2e/` dir. Friendly re-auth message when
  session is revoked.
- **YAML parser** — `e2e-tests.yaml` shape with inline-comment
  stripping. Caught a regex-escape bug earlier in the day.
- **Assertion engine** — `reply_contains`, `reply_regex`,
  `reply_min_len`, `reply_not_contains`. Cleanly fails per-test
  without crashing the whole run.
- **Summarizer** (`infra/e2e-summarize.py`) — buckets results into
  pass / assertion-fail / timeout / harness-error. The categorization
  was useful for spotting bug #2.
- **Inter-test gap of 60s** — eliminated Telegram's "suspicious
  activity" session revocation. Confirmed working across step-A v2.

---

## Recommended step B work plan

1. **First — reduce per-turn token cost** (no code changes to OpenClaw)
   - Audit AGENTS.md for further trim opportunities (target 400 lines)
   - Investigate whether OpenClaw can be configured to inject only relevant SKILL.md files based on intent rather than all 49
   - Enable prompt caching if not already on
2. **Verify Claude failover triggers on rate-limit** — read
   `openclaw.json` / model-routing config. Force a rate-limit (send
   many test messages) and confirm the failover fires within 3
   retries instead of looping on gpt-5.4-mini.
3. **Fix harness reply-detection** — filter status pings + add
   `wait_for_final` option (Bug #2).
4. **Re-run step A 3-test validation** — should now pass 3/3
   reliably across 5+ consecutive runs.
5. **Run full 13-test suite** — with bugs fixed, should be majority
   green. Failures become real signal about specific skills.

---

## How to run the harness

```bash
# One-time setup (already done; only needed if session revoked)
python3 infra/setup-telethon-session.py

# Run all tests in e2e-tests.yaml
python3 infra/e2e-harness.py

# Run a subset
python3 infra/e2e-harness.py --only weather-today,humanizer-strip,usage-today

# Save JSON results for the summarizer
python3 infra/e2e-harness.py --json /tmp/results.json
python3 infra/e2e-summarize.py /tmp/results.json
```

Inter-test gap is currently **60s** (conservative — preserves
rate-limit headroom + avoids Telegram session revocation). Drop to
15s after step B fixes land.
