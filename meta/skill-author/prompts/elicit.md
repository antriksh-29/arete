# Elicit prompts — the 4Q (create) and 3Q (refine) state machine

## CREATE mode (4 questions, with a branch at Q2)

The state machine in `meta/skill-author/scripts/elicit.ts` emits these
verbatim per turn. Wording is informed by Anthropic's `skill-creator`
template, simplified for non-technical conversation (no "tests" question
— removed per product feedback; not user-relevant).

### Q1 — Capability

> Got it, let's make a new skill. One line: what should this skill enable me to do?
>
> (Example: "send me a random motivational quote every morning",
> or "when I forward you a receipt photo, log the amount + merchant
> + date to my expense tracker".)

**Extracted field:** `capability` (free-form one-liner).

### Q2 — Invocation mode (the fork)

> Should I run this when you ask me to, or automatically on a schedule?
>
> • Reply "manual" if you'll trigger it (you'll give me the words to listen for).
> • Reply "periodic" if it should run on a schedule (daily/weekly/etc).

**Extracted field:** `invocation_mode ∈ {"manual", "periodic"}`.

### Q3 — Branches on Q2

**If `manual`:** triggers

> What 2–3 phrases should I listen for? These are the words you'll type
> to trigger this skill.
>
> (Example: "good morning quote", "give me my morning quote", "morning
> motivation". More specific = less likely to collide with a neighbor skill.)

**Extracted field:** `triggers[]` (array of 2–3 strings).

**If `periodic`:** frequency

> How often should I run it?
>
> • `hourly` — every hour
> • `daily` — once per day at a specific time (you'll pick the hour)
> • `weekly` — once per week on a specific day + time
> • `monthly` — once per month on a specific day + time
> • `custom` — give me a raw cron expression

**Extracted field:** `schedule.frequency`.

### Q4 — (periodic only) schedule details

Asked only when `invocation_mode = periodic`. The wording varies by frequency:

- **daily:** "What time of day? (24h format, IST. Example: `08:00` for 8am, `22:30` for 10:30pm.)"
- **weekly:** "What day + time? (Example: `monday 08:00` or `saturday 19:00`.)"
- **monthly:** "What day-of-month + time? (Example: `1 09:00` for 9am on the 1st.)"
- **hourly:** "Every hour, on the hour? Or at a specific minute past the hour? (Reply `on the hour` or e.g. `:15` for quarter past.)"
- **custom:** "OK, paste the raw cron expression. (5-field format: `minute hour dom month dow`. Example: `*/15 8-22 * * *` = every 15 min between 8am and 10pm.)"

**Extracted fields:** `schedule.time_of_day` / `schedule.day_of_week` / `schedule.day_of_month` / `schedule.custom_cron`.

### Q5 — Output shape

> What should the result look like? Reply in this chat, a file,
> a brain page, an email, a calendar event, something else?

**Extracted field:** `output` (free-form). Drives the generated SKILL.md
`Output Format` section.

---

## REFINE mode (3 questions)

### R1 — What's wrong

> Got it, let's tweak `<TARGET>`. What's wrong with how it works now —
> or what should it do that it doesn't?

### R2 — Desired behavior

> OK, so when this fires, what should happen instead?

### R3 — Triggers

> Keep the existing trigger phrases, or want to add new ones?
>
> (Existing triggers: `<list from source SKILL.md>`. If you add new ones,
> I'll check they don't collide with other skills.)

---

## Confirmation template (after all questions, before safety gate)

This is the natural-language summary the user sees. NEVER show YAML or
cron expressions when natural language reads better.

**For CREATE — manual mode:**

> OK, here's what I've got:
>
> When you say things like **<trigger_1>**, **<trigger_2>**, or **<trigger_3>**,
> I'll <capability_in_one_sentence>, and reply with **<output_description>**.
>
> Sound right? (Inline button: ✅ Yes, save it • ✏️ Tweak Q<which> • ❌ Cancel)

**For CREATE — periodic mode:**

> OK, here's what I've got:
>
> **Every day at 08:00 IST**, I'll send you a random motivational
> quote, and reply with a two-line message — quote then attribution.
>
> Sound right? (Inline button: ✅ Yes, save it • ✏️ Tweak • ❌ Cancel)

The "when" string is built from `schedule`:
- daily → "every day at HH:MM IST"
- weekly → "every <day-of-week> at HH:MM IST"
- monthly → "on the Nth of every month at HH:MM IST"
- hourly → "every hour at :MM"
- custom → "on schedule `<cron-expr>`"

**For REFINE:**

> OK, here's the diff:
>
> **Before:** <one-line summary of source SKILL.md behavior>
>
> **After:** <one-line summary of the desired behavior>
>
> <if additional_triggers:> New triggers added: <list>
>
> <if fork-on-modify:> This will fork `<source-tier>/<TARGET>` into
> `user-skills/<TARGET>/`. The original stays untouched.
>
> Sound right? (Inline button: ✅ Yes, save it • ✏️ Tweak • ❌ Cancel)

## After confirmation — the only TWO user-visible messages

Per product feedback, the user does NOT see safety/routing/commit/deploy
phase chatter. They see exactly TWO messages after pressing ✅:

**Message 1 (immediately after confirm, from safety-scan.ts):**

> Creating this new skill for you. Please wait for 1–2 min.

**Message 2 (~60-90s later, from the deploy-callback hook):**

> 🎉 Live. Try saying "<trigger_1>" or "<trigger_2>" — I'll know what
> to do now.
>
> (For periodic skills:)
> 🎉 Live. I'll run this automatically — first run is at <next scheduled time>.

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

> You have an unfinished draft from <relative-time>:
> "<capability_or_change_statement>"
>
> Continue from where we left off, or start fresh?
> (Inline button: ▶️ Continue • 🆕 Start fresh • 🗑️ Discard draft)
