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

## When to revisit

- A new external service is integrated (e.g., Slack, WhatsApp, voice
  notes) — does it have a brain-write path?
- A new automation is built — checklist above.
- The brain feels "stale" or "wrong" in any briefing — that's a brain-write
  contract failure somewhere; trace which pathway lost the data.
