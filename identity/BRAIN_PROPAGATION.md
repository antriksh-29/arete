# Brain propagation principle

> **Every system component that learns something about an entity MUST update
> that entity's brain page before completing its work.**

The brain is the agent's primary knowledge surface. Brain-first lookup
(rule 9 of AGENTS.md) only works if the brain is fresh. A brain that
isn't continuously updated is dead memory.

This document captures the principle, the audit of current entry points,
and the contract every new pathway must follow.

---

## The contract

For any pathway that touches an entity (person, company, meeting, idea,
note, habit, anything in `~/.brain/<type>/`), the pathway MUST do one of:

- **New entity** → create the page (frontmatter + summary stub + first
  timeline entry, citation included).
- **Known entity, new fact** → append a timeline entry to the existing
  page, bump `updated:`.
- **Known entity, no new fact** → no-op is fine.

Brain writes are not optional. They are the memory-consolidation step.
If a pathway *can't* reliably write to the brain, that pathway is
considered broken and gets fixed before it's marked shipped.

---

## Why brain-first works

The agent reads the brain before any external API call (rule 9). Quality
of every recall, every briefing, every "do you know about X" answer is
bounded by how fresh the brain is. Skipping brain-writes saves a few
tokens today but corrodes the agent's usefulness over weeks.

Concrete failure mode we hit (May 1-2): user sent a follow-up to
Kaushik on May 1; Kaushik replied with OOO. Daily collector pulled the
emails but **skipped** Kaushik because his page already existed —
"already on existing page" was treated as "no work needed." So the
brain page froze at Apr 30. Morning briefing on May 2 read the stale
page and reported "Kaushik 8 days overdue follow-up" — wrong, and
embarrassing. The fix wasn't to teach the briefing to query live Gmail
(though that's a defensive layer); the *real* fix was to keep the brain
page itself updated as new emails come in. Brain freshness IS the
foundation; live cross-checks are belt-and-suspenders.

---

## Audit — every entry point and its brain-write contract

| Entry point | Brain write? | Mechanism |
|---|---|---|
| Daily collector — INBOX emails | YES | `bootstrap-enrich.mjs` `appendTimelineEntries()` for existing entities; `enrichPeople/Companies()` for new entities. |
| Daily collector — SENT emails | YES (after fix) | Same path, but recipient extraction needs `get_message` follow-up because ClawVisor's `list_messages` omits `to:` for sent folder. |
| Daily collector — Calendar events | YES | Attendees flow through `buildPeopleMap` and either update existing pages or create new ones. |
| Live Telegram conversation | YES (inline) | AGENTS.md "Inline brain capture" block instructs the agent to capture entities during the main turn. **Must be enforced with MUST-level directives** so agents don't skip it under load. |
| Morning briefing — live Gmail/Calendar reads | YES (after fix) | Briefing prompt now ends with: "after composing, for each insight you derived from live data, call `gbrain__add_timeline_entry` on the relevant person/company page so future briefings have it." |
| Meeting-prep heartbeat | YES (after fix) | Same pattern as briefing. |
| Reminders / web search / shell tools | OPTIONAL | Low-frequency; only write if the reminder/search target is itself a tracked entity. Defer until friction observed. |

---

## Strengthening contracts

### For Telegram main-turn capture

The agent's "Inline brain capture" instruction in AGENTS.md uses the
verb MUST, not "consider." When the user message contains an entity
mention with a substantive fact (commitment, opinion, change of state,
new collaboration, dated event), the agent MUST call
`gbrain__add_timeline_entry` (or `gbrain__put_page` for new entities)
in the same turn. Skipping is permitted only for purely operational
messages ("yes", "ok", "remind me…").

If the agent emits a final reply WITHOUT calling a brain-write tool when
the message clearly contained a captureable signal, that's a failure to
log retroactively. The behavioral-eval suite has tests for this.

### For briefing/prep insight write-back

Every briefing or meeting prep that pulls live data and derives an
insight ("Kaushik is OOO till Monday", "MicroConf still hasn't replied
in 26 days", "Bhramari sent a follow-up yesterday") MUST close the loop
by writing the insight to the relevant brain page. Otherwise tomorrow's
briefing rediscovers the same fact and burns tokens.

The script template:

```
1. Read live data (calendar, gmail).
2. Cross-check with brain context.
3. Compose briefing for user.
4. For each entity-specific insight derived in step 1-2, call
   gbrain__add_timeline_entry to record the fact on that entity's page.
5. Send the briefing.
```

Step 4 is non-negotiable.

---

## Pre-ship checklist (folded into `skills/full-automation/SKILL.md`)

The full-automation skill's mandatory pre-ship checklist already covers
brain-write at the automation level (item 1: "brain page exists"). This
doc extends that to every component:

- [ ] Does this pathway touch entity information?
- [ ] If yes — what's the brain-write step? What page does it create or update?
- [ ] Is the write deterministic (script) or LLM-mediated (agent prompt)?
  Both are fine; just ensure it's wired.
- [ ] Have you tested with a fresh entity (new page) AND a known entity
  (timeline-entry append)?
- [ ] If the pathway can fail silently (rate limit, model skip), is there
  a defensive layer (retry, cron sweep, eval) that catches the gap?

---

## The full daily Gmail workflow (canonical)

This is the user-defined ideal workflow for daily Gmail. Every layer must
participate; skipping any layer breaks the loop.

```
1. PULL — last 24h sent + received emails (live, plus the 7am collector's
   2-day overlap window which writes to brain for any new signal-grade message).

2. TRIAGE — for each non-noise email:
   a) classify importance: high / medium / low
   b) extract structured facts:
      - OOO_UNTIL: <date> — person away till date
      - DEADLINE: <date> — something expected by date
      - COMMITMENT: <party> promised <thing> by <date>
      - SCHEDULED_ACTION: check-in / follow-up on <date>
      - STATE_CHANGE: role change, joined/left company, new collaboration

3. WRITE — propagate triage results:
   a) gbrain__add_timeline_entry on the relevant entity page, formatted
      with the ALL_CAPS prefix so future briefings can grep for them
   b) for each future-dated action that requires the user to do something
      on a specific day, schedule a one-shot reminder via
      `~/bin/schedule-reminder.sh "@YYYY-MM-DD 09:00:00" "<text>"` to fire
      that morning

4. SURFACE — compose the briefing:
   a) calendar today
   b) FORWARD-LOOKING brain query: any entity timeline entry mentioning
      today's date? (catches "Kaushik back from OOO today" without needing
      a reminder to fire)
   c) high-importance new emails (from step 2)
   d) open threads still stalled (cross-checked via live Gmail)
   e) FYI / lower priority — one-liner each
```

### The Kaushik-OOO worked example

May 1: Kaushik replies with OOO "I'll be back May 4."

May 2 morning briefing:
- Step 2: triage classifies as OOO_UNTIL: 2026-05-04
- Step 3a: writes to people/kaushik-immadisetty.md timeline:
  `**2026-05-02** — OOO_UNTIL: 2026-05-04 — Kaushik away till May 4; return action: follow up on Reddit pilot proposal [Source: Gmail msg <id>, 2026-05-02]`
- Step 3b: schedules `~/bin/schedule-reminder.sh "@2026-05-04 09:00:00" "Kaushik back from OOO — follow up on Reddit pilot proposal"`
- Step 4c: today's briefing surfaces "Kaushik OOO till May 4 — pilot follow-up parked till then."

May 4 morning:
- Forward-looking brain query (step 4b) finds the May 2 timeline entry on
  Kaushik's page mentioning 2026-05-04 → surfaces "Today (May 4): Kaushik
  back from OOO, you wanted to follow up on Reddit pilot."
- Scheduled reminder also fires at 9am as belt-and-suspenders.

Two layers — brain-driven AND scheduled — converge on May 4. Robust.

### Why both layers (brain-driven AND scheduled)

The brain-driven approach (forward-looking query) is the primary
mechanism. It's:
- Self-healing: even if a reminder gets lost, the brain page still has
  the entry, future briefings still surface it
- Context-rich: includes the WHY (return action: pilot proposal), not
  just a one-line ping
- Composable: the agent can reason over it during conversations ("when
  is Kaushik back?")

The scheduled reminder is the failsafe. It guarantees a notification
even if the briefing gets skipped (machine down, agent overloaded,
etc.). One-shot, fires at 9am IST regardless of brain state.

---

## When to revisit

- A new external service is integrated (e.g., Slack, WhatsApp, voice
  notes) — does it have a brain-write path?
- A new automation is built — checklist above.
- The brain feels "stale" or "wrong" in any briefing — that's a brain-write
  contract failure somewhere; trace which pathway lost the data.
