# TODO: Fix B — enforce relay discipline at the openclaw framework layer

Two regression classes share a single root cause: **the agent is given
too much freedom in what it emits to Telegram between tool calls.**
Both are documented here because both will be fixed by the same
intervention. Until then, RULE 0.7 in `identity/AGENTS.md` is
prompt-level mitigation — necessary but provably insufficient.

## Class 1 — reasoning-leak (pre-tool-call assistant text)

The agent emits internal reasoning ("The user is asking about… I should
use brain-ask… Let me run…") as a Telegram message **before** the tool
call. openclaw forwards it. The actual reply arrives after the tool
call as a second message. User sees two messages where the first is
the agent's monologue.

### Failing turn (2026-05-14, msg ids 4835 + 4836)

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

### What openclaw should do

The agent's turn shape was:
1. Receive user message
2. Emit assistant message (the reasoning) → openclaw forwards (LEAK)
3. Call `~/bin/brain-ask.py "..." "..."`
4. Tool returns `user_message`
5. Emit final assistant message (the answer) → openclaw forwards
6. Telegram shows both messages from steps 2 and 5

Bug: step 2's message should not have been forwarded. Tool messages
are already not forwarded — Fix B extends the same discipline to
assistant text emitted between tool calls.

## Class 2 — `user_message` paraphrase (post-tool-call rewriting)

The agent calls the right tool, gets the right `user_message` back, but
rewrites it before relay. Strips emoji envelopes (🔎 🕸️ 📚), converts
numbered lists to bullets, drops the trailing `📎 source: <slug>` /
`📎 sources: ...` citation lines. The content underneath is correct
but the user-facing presentation is mangled — and the citation is the
user's only click-through to the brain page, so dropping it is
functional damage, not just cosmetic.

### Failing comparison (2026-05-14, post-PR #93 Layer 3 probe)

`brain-search.py` returned this `user_message` field on stdout:

```
🔎 Found 3 matches for 'ambition':

1. ambition, survivorship bias, persistence
   Posted by: Nishank Singla (@nishanksingla)
   Founder-mode self-portrait — ambition meets reality
   📎 media/social/x-nishank-20260512

2. ...
3. ...
Ask 'quote from <#N>' / 'what does <#N> say about X' to dive in.
```

Agent emitted to Telegram:

```
Found these on ambition in your brain:

• Nishank's post on turning 34, where the core idea is being measured
  by "persistence through uncertainty, failure, and self-doubt."
```

Stripped 🔎 header, bullet-ized the numbered list, dropped per-result
📎 citations, paraphrased the title, condensed three results into one,
and folded in content from a brain-ask call. The user can't follow up
with "tell me more about #2" because there's no #2 in the reply.

Same shape regression hit `brain-graph` (🕸️ dropped, 📎 source
dropped) and `brain-compare` (📎 sources line dropped).
`brain-ask --persona` was untouched because its `user_message` is
single-prose — the agent doesn't see paraphrasing affordances there.

### What openclaw should do

When a tool returns `user_message`, openclaw should forward that
field byte-for-byte as the assistant's Telegram reply. The model's
free-form prose in the assistant message following the tool call
should NOT be forwarded — the model is allowed to think, but only the
script's user_message is user-facing. Today the model writes its own
"reply" and that gets forwarded instead of the tool's output.

Two specific behaviors needed:
1. If an assistant message immediately follows a tool result AND the
   tool result includes `user_message`, forward `user_message` and
   drop the assistant message.
2. If a tool returns `user_message` but no assistant message follows
   in the same turn, still send `user_message` (single-tool turns).

## Why prompt-level fixes failed

Three escalating revisions of RULE 0.7 over 2026-05-13 → 2026-05-14:

| Revision | Placement | Wording strength | Result |
|---|---|---|---|
| §6 TELEGRAM-SPECIFIC RULES | mid-file | "Do not narrate" | Leak persisted |
| RULE 0.7 (Fix A) | prelude block | "Tool calls are silent" + concrete examples | Leak persisted |
| RULE 0.7 byte-for-byte | prelude block | "Relay byte-for-byte" + banned-transforms checklist | Leak + paraphrase persisted |

The model reads the rule, restates it back when asked, and ignores it
on the next turn. Prompt-level enforcement has hit a ceiling for both
classes. Framework-level enforcement is now the only path.

## Probes to reproduce

### Reasoning-leak repro

```
Telegram → @alfred_antriksh_bot:
  what are the top 3 takeaways from Nishank's article? please quote
  him directly when you mention each takeaway
```

Expect TWO bot messages. The first is the leak.

### Paraphrase repro

```
Telegram → @alfred_antriksh_bot:
  find articles about ambition in my brain
```

Expect ONE bot message with no 🔎 header, no numbered list, no
per-result 📎 citation. Compare against `~/bin/brain-search.py "ambition"`
on the VPS — that prints the correct envelope on stdout. The
discrepancy is the bug.

## Regression assertions for the probe scripts (add when Fix B lands)

```python
# Reasoning-leak check — exactly one bot reply per turn for
# content-Q intents
assert len(replies_after_user_msg) == 1, (
    f"got {len(replies_after_user_msg)} bot messages; "
    f"reasoning leak likely: {replies_after_user_msg[0]['text'][:200]}"
)

LEAK_PATTERNS = [
    "I need a pointer", "Let me use `last`",
    "should relay the user_message verbatim",
    "per the reading-list skill",
    "I must use `python3 ~/bin/brain-ask.py",
    "The user is asking about", "This refers to bookmarked content",
    "The script returned an ack",
    "The skill says for keyword references",
    "I need to resolve the bookmarked source",
    "Per the note skill",
]

# Paraphrase check — the script's emoji envelope must survive relay
import subprocess
script_output = subprocess.run(
    ["ssh", "vps", "python3", "~/bin/brain-search.py", "ambition"],
    capture_output=True, text=True,
).stdout
script_msg = json.loads(script_output)["user_message"]
assert script_msg in telegram_reply, (
    f"user_message was rewritten on the way to Telegram. "
    f"script said: {script_msg[:200]!r}; "
    f"user saw: {telegram_reply[:200]!r}"
)
```

## Where the fix probably lives

Investigate in order:

1. **`~/.openclaw/openclaw.json`** — agent config; look for a
   `streamAssistantMessages`, `forwardIntermediate`, `suppressThinking`,
   or `relayPolicy` knob. Cheapest if it exists.
2. **openclaw's Telegram channel handler in source** — patch the
   message-emit path: forward `tool_result.user_message` when present,
   drop the model's subsequent free-form message in the same turn.
3. **Wrapper proxy between openclaw and Telegram** — last resort.

## Effort estimate

- 30 min to discover whether (1) exists
- 1-2 hours patch + test if it's (2) or (3)

Until B ships, RULE 0.7 is the prompt-level mitigation and the
functional state of each affected capability is documented at the
bottom of the Layer 3 PR (#91) — every script's stdout is correct,
the transport layer is where data gets re-shaped.
