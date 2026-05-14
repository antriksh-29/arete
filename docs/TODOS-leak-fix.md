# TODO: Fix B — suppress pre-tool-call assistant messages at the openclaw layer

## Why this exists

`identity/AGENTS.md` RULE 0.7 forbids the agent from narrating tool
decisions to Telegram (concrete examples below). That rule is
prompt-level — it relies on the model reading and obeying it on every
turn. The 2026-05-13 and 2026-05-14 probes both caught the agent
emitting chain-of-thought as a separate Telegram message **despite**
the rule being present in AGENTS.md `§6 TELEGRAM-SPECIFIC RULES`.
Moving it to RULE 0.7 (read on every turn) is "Fix A" — necessary but
not sufficient. The durable fix is at the framework layer, suppressing
the message before it ever reaches Telegram.

## The two-message turn shape

Failing turn from 2026-05-14 (`@alfred_antriksh_bot`, msg ids
4835 + 4836):

```
[+22.5s] id=4835 (reasoning leak — should be SUPPRESSED):
  The user again asks for top 3 takeaways from Nishank's article
  with direct quotes. This is bookmarked content, so I should use
  brain-ask. The previous resolution used `last` and returned a
  QOVES article, which may have been the wrong source for "Nishank's
  article." I need to resolve the bookmarked source more carefully.

  The skill says for keyword references like "Nishank's article",
  use free text `"Nishank's article"` and brain-ask will internally
  call brain-find to score all bookmarked rows by handle/title/url
  host. That should be better than `last`. So I'll run brain-ask
  with the exact phrase as pointer.

[+23.5s] id=4836 (the real reply — should be FORWARDED):
  1. The post is a reflection on a life milestone: "Today, I turned thirty-four."
  2. ...
  3. ...
  📎 source: media/social/x-_nishankj-20260513
```

The agent's turn shape was:
1. Receive user message
2. Emit assistant message (the reasoning) → openclaw forwards to Telegram (LEAK)
3. Call `python3 ~/bin/brain-ask.py "Nishank's article" "<question>"`
4. Tool returns `user_message`
5. Emit final assistant message (the answer) → openclaw forwards to Telegram

The bug: **step 2's message should not have been forwarded.** It's pre-tool-call reasoning, not user-visible reply.

## The fix shape

openclaw's Telegram channel handler (wherever it converts
assistant-message → `sendMessage` call) should:

- Track per-turn assistant messages
- Identify the "final" assistant message — the one emitted after the
  last tool call of the turn
- Forward ONLY the final message
- Drop intermediate assistant messages (or relegate them to a debug
  log)

Tool messages are already not forwarded — this extends the same
discipline to assistant text emitted between tool calls.

## Probe to reproduce

Use the same prompt that captured the failure. Send to
`@alfred_antriksh_bot` via the Telethon harness at
`infra/e2e-harness.py` or a one-off probe modeled on
`/tmp/probe_pr87.py`:

```
what are the top 3 takeaways from Nishank's article? please quote
him directly when you mention each takeaway
```

After Fix B ships, the Telegram chat should show ONE bot message (the
takeaways with `📎 source:`). Today (2026-05-14) it shows two
messages, where the first is reasoning.

## Probe-script regression assertion (add when Fix B lands)

```python
# Assert exactly one bot reply per turn for content-Q intents
assert len(replies_after_user_msg) == 1, (
    f"got {len(replies_after_user_msg)} bot messages; "
    f"reasoning leak likely: {replies_after_user_msg[0]['text'][:200]}"
)
```

Plus pattern-bans for the meta-commentary that should never appear:

```python
LEAK_PATTERNS = [
    "I need a pointer",
    "Let me use `last`",
    "should relay the user_message verbatim",
    "per the reading-list skill",
    "I must use `python3 ~/bin/brain-ask.py",
    "The user is asking about",
    "This refers to bookmarked content",
    "The script returned an ack",
    "The skill says for keyword references",
    "I need to resolve the bookmarked source",
]
```

## Where the fix probably lives

Three candidate surfaces — investigate in order:

1. **`~/.openclaw/openclaw.json`** — agents config; look for any
   `streamAssistantMessages`, `forwardIntermediate`, or `suppressThinking`
   knob. Tunable per-channel.
2. **Telegram channel handler in openclaw source** — if no config knob
   exists, this is where we'd patch: filter messages where
   `len(message.tool_calls) == 0` AND another tool call has already
   been emitted in the same turn → that's intermediate reasoning.
3. **Wrapper between openclaw and Telegram** — last resort: add a
   filter proxy. Cleanest is config; messiest is wrapper.

## Effort estimate

- 30 min to discover whether (1) exists
- 1-2 hours patch + test if it's (2) or (3)

Until B ships, Fix A (RULE 0.7) is the only line of defense. Monitor
each new content-Q feature (note-taking, multi-page synthesis, etc.)
for the leak via the Telethon probes — if it returns, escalate Fix B
priority.
