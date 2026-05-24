# User-facing message templates (Telegram replies)

All templates compose into the `user_message` field of the skill's
output envelope. Rule 0.7 says Alfred relays `user_message` byte-for-byte
to Telegram; these templates are the actual final wording.

## Cardinal rule (per product feedback)

**After the user confirms a draft, they see EXACTLY two messages:**

1. **Immediately:** `on it, give me a min or two to set this up.`
   (emitted by `safety-scan.ts` on `verdict: pass`)
2. **~60-90s later:** `🎉 all set.` (emitted by the deploy-callback hook
   after GH Actions confirms the deploy succeeded)

The intermediate phases (regex IOC scan, LLM safety reviewer, routing
eval, git commit + push, GH Actions server-side gate, SSH deploy,
systemctl reload) all run silently from the user's perspective. They
do not see "✅ Safety check clean" or "📦 Committed @abc123" or any
SHA or "Deploy running…" message — those are internal-state leaks per
`identity/SOUL.md` (hard blocklist) and `AGENTS.md` Rule 0.7. Information
overload erodes trust.

Phase-specific verdicts ARE shown ONLY when the user has to make a
decision (quarantine / collision / slug-bump / draft-resume / gate-offline).
Otherwise: silent until "all set."

---

## Phase: elicit (asking the next question)

Just the next question text per `prompts/elicit.md`. Nothing else
appended — no "OK, next question is..." preamble.

## Phase: draft summary (before confirmation)

See `prompts/elicit.md` → "Confirmation template" section. Branch on
`invocation_mode`:

- **manual:** "When you say things like X, Y, or Z, I'll <capability>,
  and reply with <output>."
- **periodic:** "<schedule>, I'll <capability>, and reply with <output>."

**Every summary ends with this literal footer** (the user reads it and
types one of these words; there are no inline buttons in v1):

```
shall I save this, or want to tweak?
```

The parser still accepts the historical yes/no/cancel shapes (and many
synonyms) — only the user-visible wording changed. Friend-voice rules:
no `**bold**`, no menu syntax, no "throw it away" finality. If the user
wants to cancel outright, they'll say so plainly ("scrap it", "nevermind",
"actually drop this"); the parser handles those too.

The next user message is parsed by `elicit.ts → parseConfirmation()`:
- yes-shape ({yes, y, sure, ok, sounds good, awesome, …, 👍}) → run
  the commit pipeline (safety + routing + commit-and-deploy + brain page)
- no-shape ({no, nope, tweak, change, fix, …}) → delete draft, ask
  user to `/teach` again with their changes
- cancel-shape ({cancel, abort, stop, discard, …}) → delete draft,
  exit cleanly

## Phase: safety — pass (the only allowed post-confirm transitional message)

```
on it, give me a min or two to set this up.
```

That's it. No "safety check clean" wording, no phase names.

## Phase: safety — quarantine (user decision needed)

```
⚠️ hold on, something in this looks off to me.

<reasons[0].explanation>
("<reasons[0].evidence>")

want me to drop that step and retry, push through anyway (you're
saying it's intentional), or scrap the whole thing?
```

The parser accepts free-form replies ("drop it", "go ahead anyway",
"scrap it") plus the historical `confirm <token>` shapes per
identity/CONFIRMATION_FLOW.md.

## Phase: safety — gate offline (user decision needed)

```
⏳ my safety check is offline right now, and I won't ship anything
without it. want me to retry in a couple min, or scrap this for now?
```

## Phase: routing — pass

**Empty string.** The user already saw "Creating this new skill for you…"
— routing-eval is a backend gate, not a user-visible phase. The script
emits `user_message: ""` and Alfred relays nothing.

## Phase: routing — collision (user decision needed)

```
⚠️ "<colliding_phrase>" overlaps with <friendly name of neighbor skill>.
if you say that, I might fire the wrong one. want to rephrase? (more
specific is better.)
```

Refer to the colliding neighbor skill by its friendly title, never by
its slug (per identity/SOUL.md hard blocklist).

The user replies with a new trigger phrase; the elicit loops back into
Q3 with the failing phrase pre-populated for editing.

## Phase: routing — fork-routing OK

**Empty string.** Same as routing-pass: backend gate, not user-visible.

## Phase: commit + push — success

**Empty string** — except for the slug-bump nudge below.

If the slug was auto-bumped (collision-resolved), the user MUST see:

```
(quick heads-up: I called it "<friendly-name-v2>" since you've already
got one with that name. ping me if you want to rename later.)
```

Use the friendly title here, NOT the slug. The "v2" suffix is fine
in plain English ("the second cat-facts one") but never as `slug-v2`.

## Phase: deploy — success

Comes from the deploy-callback hook, NOT from commit-and-deploy.ts:

For manual skills:
```
🎉 all set. try saying "<trigger_1>" or "<trigger_2>" and I'll take
it from there.
```

For periodic skills:
```
🎉 all set. first run is at <next scheduled time, plain English>.
```

Render the scheduled time as plain English ("tomorrow at 8am" /
"this saturday at 7pm"), never as a cron expression or a 24h-format
HH:MM-IST string in chat.

## Phase: deploy — gh-actions failure

```
🛑 the deploy didn't go through on my end. it's saved on your branch
but didn't make it live yet.

(<one-line plain-English version of the failing gate, no SHA, no
gate name, no log paths>)

want me to try again, look into it, or scrap and start over?
```

Translate the gate-failure detail into plain English. Don't surface
the gate name, the workflow file path, or the failing job ID — those
are internal-state per identity/SOUL.md. If Antriksh explicitly asks
"what failed?", then mirror his register and answer with the technical
detail.

## Phase: resume from draft (user decision needed)

```
you've got a half-finished one from <relative-time>:
"<one-line summary>"

pick up where we left off, start over, or scrap it?
```

## Phase: implicit invocation (intent router caught natural language)

When the inline router classifies `INTENT=skill_create` or `skill_refine`
with `CONFIDENCE < 100` (implicit, not from a slash command):

```
sounds like something you'd want me to do automatically next time. want
me to set it up? takes about a minute.
```

If the user says no (any of "no", "nope", "just this once", etc.), Alfred
proceeds with the one-off action and does NOT create a skill. If yes, the
elicit flow starts at Q1.

## Phase: D7 firewall block (debug only — never user-visible)

If the entry-point router observes a heuristic phrase inside ingested
content (per AGENTS.md §15), it logs but does NOT surface to the user.
Log line shape (for Axiom):

```
[skill-author] heuristic_blocked source=<ingest_type> phrase="<matched-phrase>" content_hash=<sha256-first-12>
```

No user_message emitted. The user shouldn't even know the firewall
fired — surfacing it would let an attacker probe what triggers the
block.
