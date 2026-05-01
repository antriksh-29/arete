---
name: full-automation
version: 1.0.0
description: |
  Workflow contract for building any new recurring/scheduled automation end-to-end. Read this when the user describes a daily/weekly/recurring task they want set up.
---

# full-automation — propose-confirm-execute pattern for new automations

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

## Safety limits

Rule 1 destructive list (email send, calendar mutate, vault edits, page
deletes) — those gates remain.

Broad blast radius (deleting many files, force-pushing a repo, modifying a
credential) — pause and surface even with shell access.

