---
name: full-automation
version: 1
script: null
mutating: true
cron: null
eval: skills/full-automation/eval.yaml
last_audited: 2026-05-16
description: |
  Workflow contract for building a new recurring/scheduled automation
  end-to-end on the VPS — script, systemd timer, brain page, skill doc,
  AGENTS.md hint, verification. Read this when the user describes a
  daily/weekly/recurring task they want set up.
  WHEN: "set up a daily X", "I want a weekly Y", "schedule a recurring
  Z", "automate <task> every <interval>", "build me a habit tracker
  for X", "ping me every morning with Y".
  NOT WHEN: one-shot reminders (use reminders skill); non-recurring
  scripts; modifying an EXISTING automation (just go edit it).
trigger: |
  - "set up a daily/weekly/hourly <task>"
  - "automate <task> every <interval>"
  - "build me a habit tracker for X"
  - "schedule a recurring <task>"
  - "I want X to fire automatically when <condition>"
writes_to: [habits, originals]
brain_dirs_read: []
mutates_external_state: true   # creates systemd timers + scripts on VPS
---

# /full-automation — propose-confirm-execute pattern for new automations

You have unrestricted shell access (tools.exec.security=full) on the VPS as
botuser (non-root, scoped to home directory and systemd user units).

When the user describes a recurring or scheduled automation:

1. **Restate the problem** in your own words. One sentence.

2. **Propose the solution** concretely: what brain page (if any), what script
   path, what systemd timer schedule, what the cron will do. Include the
   message template if it pings on Telegram. Show this as a small checklist
   so the user can mentally tick each item.

3. **Wait for confirmation.** Plain "yes / sounds good / go ahead / proceed"
   IS sufficient — this is operator-trusted automation work, not a
   rule-1 destructive action. Rule 1 still applies for: sending email,
   mutating calendar, deleting brain pages, modifying credentials, or
   anything with externally-visible effects.

4. **Execute end-to-end.** Do NOT split into "now you do X". You write the
   script, you `systemd-run`/install the unit, you `systemctl --user enable
   --now <unit>.timer`, you confirm the timer is listed, you reply with the
   absolute first-fire time in IST. Persistent units go in
   `~/.config/systemd/user/`. One-shot reminders go through
   `~/bin/schedule-reminder.sh`.

5. **Check it works.** If the timer fires within the next 30 min, wait and
   verify. If it fires later, simulate by running the service unit once
   (`systemctl --user start <unit>.service`) and confirm the side-effect
   (Telegram message arrived, brain page updated, etc).

6. **Capture the artifact in the brain.** Append a timeline entry to the
   relevant brain page (e.g. `habits/<name>`) noting which timer was
   wired, what it does, and how to disable it.

## Standard locations

- New helper scripts: `~/bin/<name>.sh`, `chmod +x` after writing.
- systemd user units: `~/.config/systemd/user/<unit>.{service,timer}`,
  followed by `systemctl --user daemon-reload && systemctl --user enable
  --now <unit>.timer`.
- Persisted state for an automation: `~/.brain-bootstrap-raw/state/<feature>/`.
- Brain pages always under `~/.brain/<type>/<slug>.md`.

Don't ask the user to paste shell commands. Full shell access exists
specifically so you can do the whole thing yourself.


## Pre-ship checklist (MANDATORY — every automation must pass)

Before you tell the user "shipped", verify all 6 items below. Skipping any
of these is the bug pattern that hit us with water-log readback (logging
worked, readback failed because the skill only documented write).

### 1. Brain page exists (compiled / narrative view)

Every automation gets a brain page at `~/.brain/<type>/<slug>.md`. The
page has:
- Frontmatter (`type`, `name`, `created`, `updated`, `tags`)
- Summary paragraph
- Recent activity / log table (depending on data shape)
- Stats / current state (computed by the script, stamped into the page)

This is the surface the agent finds when doing brain-first lookups
("how am I doing on X", "what habits do I track"). Never skip the brain
page even if filesystem state already exists — the brain page is what
makes the data discoverable.

### 2. Filesystem state for high-frequency events (only when needed)

If the automation logs >1 event per day (water intake, exercise reps,
expense entries), keep the raw audit trail at
`~/.brain-bootstrap-raw/state/<feature>/`. The brain page summarizes;
the filesystem holds the raw.

For low-frequency automations (one event per day or less, e.g. reading
check-in), filesystem state is optional — the brain page can hold both
log table and stats directly.

### 3. Script has BOTH write AND read subcommands

Every habit/log script MUST expose at least:
- `<script> log <args>` — record an event
- `<script> total [date]` (or `summary`, `stats`) — read back the count / state

If the user asks "how much / how many / how am I doing on X" and the
script only has `log`, the agent will fail (no path to readback). This
is the water-log bug verbatim. Don't ship a write-only automation.

### 4. Skill documents BOTH paths

`skills/<skill-name>/SKILL.md` (or `skills/habits/SKILL.md` if it's a
habit) MUST document both the write trigger AND the readback trigger,
with example commands. The agent only knows what the skill tells it.

Self-check: read your own skill file. Could a fresh agent answer "how am
I doing on X today" using only what's documented? If no, add the
readback section.

### 5. AGENTS.md capability hint is directive

If the automation introduces a new natural-language pattern, add or
update the "Capabilities you have" line in AGENTS.md to be DIRECTIVE,
not passive. Use "READ skills/<x>/SKILL.md when user asks about Y" not
"see skills/<x>/SKILL.md". Passive verbs don't make Haiku/Sonnet
proactively load the skill file.

### 6. Real-world verification (at least one of each path)

Before declaring shipped:
- Trigger the WRITE path with a realistic message and confirm side-effect
  (filesystem entry, brain page update, Telegram confirmation)
- Trigger the READ path with a realistic question and confirm the agent
  returns the right number with the right citation

If either fails, the automation isn't ready. Do not ship.

## Safety limits

Rule 1 destructive list (email send, calendar mutate, vault edits, page
deletes) — those gates remain.

Broad blast radius (deleting many files, force-pushing a repo, modifying a
credential) — pause and surface even with shell access.
