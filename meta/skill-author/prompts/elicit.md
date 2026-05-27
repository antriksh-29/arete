# Elicit prompts — the 4Q (create) and 3Q (refine) state machine

## CREATE mode (4 questions, with a branch at Q2)

The state machine in `meta/skill-author/scripts/elicit.ts` emits these
verbatim per turn. Wording is informed by Anthropic's `skill-creator`
template, simplified for non-technical conversation (no "tests" question
— removed per product feedback; not user-relevant).

### Q1 — Capability

> nice, let's set this up. one line: what should I do for you?
>
> (like "send me a motivational quote every morning", or "when I
> forward a receipt photo, log the amount, merchant, and date to my
> expenses".)

**Extracted field:** `capability` (free-form one-liner).

### Q2 — Invocation mode (the fork)

> should this run when you ask, or on a schedule?
>
> (say "when I ask" or "on a schedule".)

**Extracted field:** `invocation_mode ∈ {"manual", "periodic"}`.
The parser also accepts "manual" / "periodic" verbatim for back-compat.

### Q3 — Branches on Q2

**If `manual`:** triggers

> cool. what 2-3 phrases should I listen for? (more specific is
> better, so it doesn't collide with anything else I do.)
>
> (like "morning quote", "give me my morning quote", "kick me off
> with a quote".)

**Extracted field:** `triggers[]` (array of 2–3 strings).

**If `periodic`:** frequency

> how often? (say hourly, daily, weekly, or monthly, or give me a
> specific rhythm if it's something else.)

**Extracted field:** `schedule.frequency`.

### Q4 — (periodic only) schedule details

Asked only when `invocation_mode = periodic`. Friend-voice wording per
frequency:

- **daily:** "what time? (24h, IST. like `08:00` for 8am or `22:30` for 10:30pm.)"
- **weekly:** "which day and time? (like `monday 08:00` or `saturday 19:00`.)"
- **monthly:** "which day of the month, and what time? (like `1 09:00` for 9am on the 1st.)"
- **hourly:** "on the hour, or some other minute past? (say `on the hour` or `:15`, etc.)"
- **custom:** "paste the timing you want. plain English is fine, I'll figure it out — like 'every 15 min between 8am and 10pm'."

**Extracted fields:** `schedule.time_of_day` / `schedule.day_of_week` / `schedule.day_of_month` / `schedule.custom_cron`.

### Q5 — Output shape

> where should the result land? back here in chat, or somewhere else
> (file, note, email, calendar, etc)?

**Extracted field:** `output` (free-form). Drives the generated SKILL.md
`Output Format` section.

---

## REFINE mode (3 questions)

### R1 — What's wrong

> on it. what's not working about your <friendly skill name> right now,
> or what should it do that it doesn't?

(Refer to the skill by what the user calls it, never by its slug. If
the title in the frontmatter is friendly, use that. Otherwise infer
from the capability line.)

### R2 — Desired behavior

> got it. when it runs, what should happen instead?

### R3 — Triggers

> keep the existing trigger phrases, or want to add new ones?
>
> (right now I'm listening for: <list from source SKILL.md>.)

---

## Confirmation template (after all questions, before safety gate)

This is the natural-language summary the user sees. NEVER show YAML or
cron expressions when natural language reads better.

**For CREATE — manual mode:**

> got it. when you say <trigger_1>, <trigger_2>, or <trigger_3>, I'll
> <capability_in_one_sentence>, and reply with <output_description>.
>
> shall I save this, or want to tweak?

**For CREATE — periodic mode:**

> got it. every day at 8am IST, I'll send you a random motivational
> quote (quote, then attribution).
>
> shall I save this, or want to tweak?

The "when" string is built from `schedule` (always plain English, never
a cron expression):
- daily → "every day at H:MMam/pm IST"
- weekly → "every <day-of-week> at H:MMam/pm IST"
- monthly → "on the Nth of every month at H:MMam/pm IST"
- hourly → "every hour at :MM"
- custom → translate to natural language; never paste the raw expression
  into chat (see identity/SOUL.md rule 6).

**For REFINE:**

> got it, tweaking your <friendly skill name>.
>
> before: <one-line summary of source SKILL.md behavior>
> after: <one-line summary of the desired behavior>
>
> <if additional_triggers:> also listening for: <list>
>
> shall I save this, or want to tweak?

(Fork notes — that the change forks the upstream skill — are an
internal-state change, NOT user-facing. Don't surface "this will fork
`<source-tier>/<TARGET>` into `user-skills/...`" in chat; the user
doesn't care which tier owns the file.)

## After confirmation — the only TWO user-visible messages

Per product feedback, the user does NOT see safety/routing/commit/deploy
phase chatter. They see exactly TWO messages after saying "yes":

**Message 1 (immediately after confirm, from safety-scan.ts):**

> on it, give me a min or two to set this up.

**Message 2 (~60-90s later, from the deploy-callback hook):**

> 🎉 all set. try saying "<trigger_1>" or "<trigger_2>" and I'll take
> it from there.
>
> (For periodic skills:)
> 🎉 all set. first run is at <next scheduled time, plain English>.

The intermediate gate verdicts (safety pass/fail, routing collision,
commit SHA, GH Actions status) are logged to Axiom and the brain page
but never shown in Telegram. The user trusts the bot OR they don't —
showing technical proof in chat is information overload, not reassurance.

The exceptions where the user MUST see a phase-specific message:

1. **Safety quarantine** — names the suspicious step + offers options.
2. **Routing collision** — names the colliding skill + asks for rephrase.
3. **Slug auto-bump** — explains the rename.
4. **Resume from draft** — offers continue / restart / discard.
5. **Safety gate offline** — "retry in a few minutes" message.

Everything else: silent until "Live."

## Resume-from-draft template

If a draft exists at `.context/skill-author-drafts/<slug>.json` and the
user starts a new `/teach` or `/refine` turn:

> you've got a half-finished one from <relative-time>:
> "<capability_or_change_statement>"
>
> pick up where we left off, start over, or scrap it?
