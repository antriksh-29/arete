# User-facing message templates (Telegram replies)

All templates compose into the `user_message` field of the skill's
output envelope. Rule 0.7 says Alfred relays `user_message` byte-for-byte
to Telegram; these templates are the actual final wording.

## Cardinal rule (per product feedback)

**After the user confirms a draft, they see EXACTLY two messages:**

1. **Immediately:** `Creating this new skill for you. Please wait for 1–2 min.`
   (emitted by `safety-scan.ts` on `verdict: pass`)
2. **~60-90s later:** `🎉 Live.` (emitted by the deploy-callback hook
   after GH Actions confirms the deploy succeeded)

The intermediate phases (regex IOC scan, LLM safety reviewer, routing
eval, git commit + push, GH Actions server-side gate, SSH deploy,
systemctl reload) all run silently from the user's perspective. They
do not see "✅ Safety check clean" or "📦 Committed @abc123" or any
SHA or "Deploy running…" message. Information overload erodes trust.

Phase-specific verdicts ARE shown ONLY when the user has to make a
decision (quarantine / collision / slug-bump / draft-resume / gate-offline).
Otherwise: silent until "Live."

---

## Phase: elicit (asking the next question)

Just the next question text per `prompts/elicit.md`. Nothing else
appended — no "OK, next question is..." preamble.

## Phase: draft summary (before confirmation)

See `prompts/elicit.md` → "Confirmation template" section. Branch on
`invocation_mode`:

- **manual:** "When you say things like X, Y, or Z, I'll <capability>,
  and reply with <output>. Sound right?"
- **periodic:** "<schedule>, I'll <capability>, and reply with <output>.
  Sound right?"

## Phase: safety — pass (the only allowed post-confirm transitional message)

```
Creating this new skill for you. Please wait for 1–2 min.
```

That's it. No "safety check clean" wording, no phase names.

## Phase: safety — quarantine (user decision needed)

```
⚠️ Hold on — I found something in the draft that worries me:

   <reasons[0].explanation>
   Evidence: "<reasons[0].evidence>"

   What do you want to do?
   • ✂️ Drop just that step and try again
   • 🤝 You confirm it's intentional and I should proceed anyway
   • ❌ Cancel the whole thing
```

The inline buttons map to `confirm <token>` replies per
identity/CONFIRMATION_FLOW.md.

## Phase: safety — gate offline (user decision needed)

```
⏳ My safety reviewer is offline right now. I won't ship a skill
   without that check.

   • 🔁 Retry in a couple minutes
   • ❌ Cancel and start over later
```

## Phase: routing — pass

**Empty string.** The user already saw "Creating this new skill for you…"
— routing-eval is a backend gate, not a user-visible phase. The script
emits `user_message: ""` and Alfred relays nothing.

## Phase: routing — collision (user decision needed)

```
⚠️ Your trigger "<colliding_phrase>" would route to <neighbor-skill>
   instead of the new skill.

   <reasoning>

   Rephrase the trigger? (Suggest something more specific.)
```

The user replies with a new trigger phrase; the elicit loops back into
Q3 with the failing phrase pre-populated for editing.

## Phase: routing — fork-routing OK

**Empty string.** Same as routing-pass: backend gate, not user-visible.

## Phase: commit + push — success

**Empty string** — except for the slug-bump nudge below.

If the slug was auto-bumped (collision-resolved), the user MUST see:

```
(Quick heads-up: I named it `<slug-v2>` since you already have a skill
with that base name. Say `/refine <slug-v2>` if you want to rename it later.)
```

## Phase: deploy — success

Comes from the deploy-callback hook, NOT from commit-and-deploy.ts:

For manual skills:
```
🎉 Live. Try saying "<trigger_1>" or "<trigger_2>" — I'll know what
   to do now.
```

For periodic skills:
```
🎉 Live. I'll run this automatically — next run is at <next scheduled time>.
```

## Phase: deploy — gh-actions failure

```
🛑 The deploy failed at the server-side <gate_name> check. The skill
   is on your branch but didn't make it to production.

   <one-line excerpt from the failing gate>

   • 🔁 Try again (could be a flake)
   • 🛠 Look into it manually
   • ❌ Revert and start over
```

## Phase: resume from draft (user decision needed)

```
You had an unfinished <create|refine> from <relative-time>:
"<one-line summary>"

   • ▶️ Continue where we left off
   • 🆕 Start fresh (the old draft will be deleted)
   • 🗑️ Throw away the draft, don't author anything right now
```

## Phase: implicit invocation (intent router caught natural language)

When the inline router classifies `INTENT=skill_create` or `skill_refine`
with `CONFIDENCE < 100` (implicit, not from a slash command):

```
Sounds like you'd want this saved as a skill so I'd do it automatically
next time. Want me to set that up? Takes about a minute.

   • ✅ Yes, let's make a skill
   • ❌ No, just do it this once
```

If the user says no, Alfred proceeds with the one-off action and does
NOT create a skill. If yes, the elicit flow starts at Q1.

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
