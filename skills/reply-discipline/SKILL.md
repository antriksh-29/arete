---
name: reply-discipline
version: 1
script: infra/reply-discipline.py
mutating: false
cron: null
eval: skills/reply-discipline/eval.yaml
last_audited: 2026-05-08
description: |
  Constraint skill — runs before every outbound Telegram message to
  catch the failure modes that prompted recent NO_REPLY / banned-phrase
  / empty-ack fix commits. Deterministic, sub-millisecond, no LLM, no
  network. The agent does NOT route to this skill on user intent —
  the harness invokes it as a filter on every reply.
  WHEN: every outbound reply, before send.
  NOT WHEN: never user-fired; never invoked by the resolver.
trigger: |
  (constraint skill — fires on every outbound reply via the harness, not
  on user utterance)
writes_to: []
brain_dirs_read: []
constraint_skill: true
---

# /reply-discipline

A deterministic gate. Takes a candidate reply on stdin and returns one
of two envelopes:

```json
{"ok": true, "reply": "<unchanged>"}
{"ok": false, "reason": "<why>", "suggestion": "<how to rewrite>"}
```

When `ok: false`, the harness's job is to either retry the model with
the suggestion appended to the system prompt, or fall through to a
known-good template. Never send a rejected reply.

## What it catches

| Pattern | Reason | Source commits |
|---|---|---|
| Literal `NO_REPLY` | sentinel from openclaw runtime — never user-facing | `aae3222`, `b308073` |
| "no extra message needed" / "nothing more from me" / "no reply needed" | non-reply phrases — produce real content | `e919121` |
| "i have nothing (more / else) to say/add" | same | `e919121` |
| "the (sub-)agent handled it/this" | sub-agents do background brain writes; main agent owes a user reply | `aae3222`, `e919121` |
| Empty / whitespace-only | no reply at all | `aae3222` |
| Bare single-word acks ("noted", "captured", "ok", "done", "ack") with total length < 12 chars | looks broken; needs concrete content | `e919121` |

## What it does NOT catch (by design)

- Style / voice issues — that's the behavioral eval's job.
- Factual errors — that's brain-pull discipline + per-skill eval.
- Long replies that bury the answer — that's prompt design.
- Closure tone — that's the agent's prompt; we don't enforce a closer
  shape because a forced closer is its own failure mode.

This skill catches *structural* non-replies. Anything beyond that is
behavioral, not structural, and lives in `eval/behavioral-eval.yaml`.

## How to invoke

```bash
echo "<candidate reply>" | python3 ~/bin/reply-discipline.py
python3 ~/bin/reply-discipline.py --smoke
```

## Wiring (Wave 4B-β)

The deterministic gate now ships as a complete tool chain:

| Layer | File | What it does |
|---|---|---|
| Filter | `infra/reply-discipline.py` | Pure regex check; returns `{ok, reason, suggestion}` |
| Send wrapper | `infra/reply-send-wrapper.sh` | Pipes candidate through filter; sends via Telegram API only on `ok: true`; surfaces rejection envelope on `ok: false` |

Usage on the VPS:

```bash
echo "<candidate text>" | ~/bin/reply-send-wrapper.sh <chat_id>
# exit 0 → sent, prints {"ok":true,"sent":true,"chat_id":...,"message_id":...}
# exit 1 → reply-discipline rejected, prints the rejection envelope
# exit 2 → Telegram API failed
# exit 3 → usage / config error
```

### How to wire it into OpenClaw

Two options, ordered by cleanliness:

1. **Replace the bot's send call.** Find OpenClaw's outbound Telegram
   call site (likely in the gateway daemon's reply path) and pipe the
   candidate text through `reply-send-wrapper.sh` instead of calling
   `sendMessage` directly. This is the right long-term fix.

2. **Run as a post-hoc audit.** Tail the openclaw gateway journal,
   pipe each reply candidate through `reply-discipline.py` async,
   alert on rejections. Doesn't prevent bad replies, only flags them.
   Useful as a low-risk first step.

Until either is wired on the VPS, the agent's constitutional NO_REPLY
ban (~10 lines in AGENTS.md) is the runtime guarantee. The wrapper is
ready to go; deployment is a one-line edit to the gateway send call.

## Why a deterministic skill instead of a prompt rule

Per Blog 5: every "the bot got that wrong" should end as a script with
tests, not as more lines in AGENTS.md. The prompt rule (don't say
NO_REPLY) failed three times across recent commits before we wrote
this. A deterministic regex never forgets.
