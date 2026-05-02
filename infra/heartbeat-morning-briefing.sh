#!/bin/bash
# Morning briefing heartbeat — runs at 8am IST daily.
# Triggers an agent turn that runs the full triage + brain-write + briefing
# workflow (per identity/BRAIN_PROPAGATION.md).

set -e
export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/heartbeat-$(date +%Y-%m-%d).log"

echo "[$(date -Iseconds)] morning briefing start" >> "$LOG"

TODAY_IST=$(TZ=Asia/Kolkata date +%Y-%m-%d)

PROMPT="Run my morning briefing for today ($TODAY_IST IST). Compose ONE final reply at the end — no streamed 'let me check' narrations.

**SCOPE: Gmail + Calendar + people/companies/projects open threads ONLY.**

Do NOT include any of the following — they have their own dedicated briefing paths and don't belong here:
- Habits (water intake, reading streaks, exercise, etc. — anything under habits/* in the brain)
- Health / fitness / consumption metrics
- The reading check-in (fires separately at 10am IST via reading-checkin.timer)
- Water log status (separate path)
- Any 'how am I doing on X habit' summary

If a brain query returns habits/* pages, filter them out. The user explicitly does not want habit data in this briefing.

Order of operations (do all of these BEFORE writing the user-facing briefing):

1. **Calendar today + tomorrow** — pull from both Google Calendar accounts via clawvisor__calendar_list_events.

2. **Live email triage** — pull last 24h via clawvisor__gmail_search 'newer_than:1d'. For each non-noise message (skip newsletters, transactional, automated, marketing), do all of the following:

   a) **Classify importance:** high (action needed from me, response from a known counterparty, time-sensitive), medium (FYI but relevant to a tracked entity), low (catch-up reading, can defer).

   b) **Extract structured facts.** Look for and capture:
      - OOO / out-of-office windows: 'I'll be back on <DATE>', 'OOO till <DATE>', 'returning <DATE>'
      - Specific commitments with deadlines: 'I'll send X by <DATE>', 'expect this by <DATE>'
      - Scheduled actions: 'follow up with me on <DATE>', 'check back <DATE>'
      - State changes: someone changed roles, joined/left a company, new collaboration kicked off

   c) **Write structured facts to the brain** (per identity/BRAIN_PROPAGATION.md):
      For each fact about a known entity, call gbrain__add_timeline_entry on that entity's brain page with the structured fact. Format the entry to make the structure greppable:
      Examples:
        '**$TODAY_IST** — OOO_UNTIL: 2026-05-04 — Kaushik away till May 4; return action: follow up on Reddit pilot proposal [Source: Gmail msg <id>, $TODAY_IST]'
        '**$TODAY_IST** — DEADLINE: 2026-05-10 — Tracy expecting MicroConf renewal decision by May 10 [Source: Gmail msg <id>, $TODAY_IST]'
        '**$TODAY_IST** — COMMITMENT: Antriksh promised pilot proposal to Kaushik by 2026-04-28 (overdue) [Source: Gmail msg <id>, $TODAY_IST]'
      The ALL_CAPS prefix (OOO_UNTIL, DEADLINE, COMMITMENT) makes future brain queries easy.

   d) **Schedule date-bound reminders.** For each future-dated action that needs ME to do something on a specific day, call exec ~/bin/schedule-reminder.sh '@YYYY-MM-DD 09:00:00' '<concise reminder text>' to fire that morning. Examples:
      - Kaushik OOO till May 4 → schedule reminder for @2026-05-04 09:00:00 'Kaushik back from OOO — follow up on Reddit pilot proposal'
      - You promised X by May 10 → schedule reminder for @2026-05-09 09:00:00 'Pre-deadline: send X to Y today (deadline tomorrow)'
      Don't schedule duplicate reminders — if you already scheduled one for the same fact in a prior briefing, the brain page should show the prior entry, skip.

3. **Forward-looking brain query (structured timeline) — people/companies/projects only.** Find any prior brain entries that point at today as the target date.

   Strategy: gbrain__list_pages with type=person, type=company, type=project. **EXCLUDE type=habit and any path under habits/*.** For each in-flight entity, call gbrain__get_timeline with that slug and scan the returned entries for any that mention today's date '$TODAY_IST' or any of the prefix tags (OOO_UNTIL, DEADLINE, SCHEDULED_ACTION) where the date matches today.

   Cheap shortcut: for the most-active entities (those mentioned in the recent open-threads brain query in step 4 below), check timeline FIRST. If you find an entry whose date matches today, surface it in the briefing's '⏰ Today's check-ins' section.

   Plus belt-and-suspenders: gbrain__search '$TODAY_IST' as a fallback in case any text-indexed entries match. **If the result is a habits/* page, drop it.**

4. **Open threads still stalled — people/companies/projects only.** gbrain__query 'follow-up promised' — find entities with prior commitments that haven't moved in 3+ days. **Skip any habits/* matches.** For each remaining entity, run a quick clawvisor__gmail_search 'from:<email> OR to:<email> newer_than:3d' to confirm they're truly stalled (catches new activity the brain hasn't ingested yet).

5. **Compose the user-facing briefing** as ONE structured Telegram message:

   📅 **Calendar** — today's events with attendees (do brain lookup on each non-self attendee for context)
   ⏰ **Today's check-ins from prior context** — items from step 3 (Kaushik back today, your promised X is due, etc.)
   📥 **High-importance new emails** — from step 2(a), grouped by sender / topic
   🔄 **Open threads worth your attention** — from step 4, only the ones that survived the live cross-check
   📝 **FYI / lower priority** — one-line list, no detail

   Keep tight. Skip sections that are empty.

Side effects (silent — don't mention in user-facing reply):
- Brain timeline entries written for every extracted fact (step 2c)
- Date-bound reminders scheduled for every future action (step 2d)

Tomorrow's briefing builds on what you wrote today. Skipping the brain writes / reminders breaks that compounding."

openclaw agent \
  --agent main \
  --channel telegram \
  --reply-to telegram:775188536 \
  --deliver \
  --message "$PROMPT" >> "$LOG" 2>&1

echo "[$(date -Iseconds)] morning briefing done" >> "$LOG"
