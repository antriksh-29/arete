---
name: book-reservation
version: 1
script: infra/book-reservation.py
mutating: true
mutates_external_state: true
cron: null
eval: skills/book-reservation/eval.yaml
last_audited: 2026-05-12
description: |
  End-to-end restaurant / hotel / event reservation. Parses venue +
  time + party from the user message, finds the booking URL
  (Google Maps Reserve first, then Zomato → District → EazyDiner →
  Swiggy → restaurant's own site), drives the booking widget via
  fill-form.py, sends a Telegram screenshot at each step, stops
  before the final confirm click for user review, and waits for the
  user to paste an OTP when an OTP screen is reached. Owned by this
  skill — AGENTS.md routes all booking intents here so the bot never
  bails with "call directly".
  WHEN: "book at X" / "reserve at X" / "make a reservation" /
  "book a table at X" / "reserve a table for N" / "book Uru for
  2 tonight at 8 PM" / any message combining (book|reserve|reservation)
  + a venue or party-size.
  NOT WHEN: User is asking ABOUT the venue ("is Uru open?" / "what's
  Uru's menu") — that's a query, route differently. NOT WHEN there's
  no venue named — ask once for the venue first.
trigger: |
  - "book a table at <X>" / "book at <X>" / "book <X> for <N>"
  - "reserve <X> for <N> people tonight at <T>"
  - "make a reservation at <X>"
  - "reserve a table for <N>" / "table for <N> at <X>"
  - "i want to book / i'd like to book / can you book"
  - resume continuation: "OTP is <digits>" / "<6 digits>" after
    a prior booking that paused on an OTP screen
writes_to: [/tmp/booking-state-*.json]
brain_dirs_read: []
trigger_guard: any_text
---

# /book-reservation

End-to-end restaurant booking. The agent invokes this script with the
user's full message; the script handles everything from URL discovery
through OTP relay through pre-confirm screenshot.

## Agent script of record

1. Identify booking intent in the user's message (verbs: book, reserve,
   make a reservation, table for N, etc.).
2. Write the full user message verbatim to `/tmp/book-msg-<ts>.txt`.
3. Run:
   ```bash
   python3 ~/bin/book-reservation.py "$(cat /tmp/book-msg-<ts>.txt)"
   ```
4. The script auto-delivers screenshots via send-telegram-photo.sh
   throughout the flow. You do NOT need to relay them manually.
5. Relay the envelope's `user_message` field verbatim as your reply.

### OTP / resume turns

If the script's prior turn returned `state=otp_pending`, your NEXT
booking-related user message will likely be a 4-8 digit OTP code,
possibly with prefix "OTP is", "code:", or just the digits.

Pass it as the resume command:
```bash
python3 ~/bin/book-reservation.py --resume "$(cat /tmp/last-otp.txt)"
```

The script picks up the most recent `state=otp_pending` envelope,
types the OTP into the awaiting field via fill-form, and continues.

## Banned bail-out responses

NEVER respond with these as a first turn to a booking intent:

- "Call +91 ... directly — fastest way"
- "Calling is the most reliable path"
- "Given the time-sensitive nature, your fastest option is to call"
- "Web booking is blocked / requires app login" (without running the
  script first)
- "District: no booking available for this venue" (without trying)
- Any text reply that doesn't include a book-reservation.py invocation

If you respond with text-only to a booking-INTENT message you have
FAILED the task. The user explicitly chose automated booking; the
phone path is a LAST resort the script surfaces ONLY after all
platforms have produced documented failures.

## Platform priority

Tried in this order, falling through ONLY on documented failure
(0 fields filled OR explicit error from fill-form):

| # | Platform | URL pattern |
|---|---|---|
| 1 | **Google Maps Reserve** | `https://www.google.com/maps/reserve/v/dine/c/<id>?source=pa&hl=en-IN` — universal aggregator, surfaces EazyDiner / Swiggy / OpenTable times. ALWAYS first. |
| 2 | **District by Zomato** | `https://www.district.in/dining/<city>/<slug>/book?date=<YYYY-MM-DD>&guests=<N>` |
| 3 | **Zomato direct** | `https://www.zomato.com/<city>/<slug>/book` |
| 4 | **EazyDiner** | `https://www.eazydiner.com/<city>/<slug>` |
| 5 | **Swiggy Dineout** | search → first swiggy.com/dineout result |
| 6 | **Restaurant's own site** | search → main domain → look for Reserve / Book CTA |
| 7 | **Phone / email** | LAST resort, only after #1-6 documented failures |

URL discovery is via Google search inside the script (no MCP needed).
The script reuses fill-form.py for all actual form drives — this
skill is the orchestrator, fill-form is the executor.

## OTP handling

Permitted per AGENTS.md §2 Exception 2 — user-originated OTPs in
chat are treated as form answers. When the booking screenshot shows
an OTP entry screen:

1. Script saves state to `/tmp/booking-state-<ts>.json` with
   `state=otp_pending`, the URL, the OTP field selector hint,
   and remaining answers.
2. Script sends Telegram message: "OTP screen reached — paste the
   code you received".
3. Script EXITS (turn ends).
4. When user replies with the OTP, the agent calls
   `book-reservation.py --resume "<otp digits>"` which loads the
   most recent state file and continues the fill via fill-form.

## Output envelope

Single-line JSON on stdout. Key fields:

```json
{
  "ok": true,
  "state": "completed | otp_pending | confirm_ready | failed | all_platforms_failed",
  "platform": "gmr | district | zomato | eazydiner | swiggy | own_site | phone",
  "venue": "Uru Brewpark",
  "party": 2,
  "time": "8:00 PM",
  "date": "2026-05-12",
  "url_tried": "https://...",
  "screenshots": ["/tmp/booking-step-0.png", ...],
  "state_file": "/tmp/booking-state-<ts>.json",
  "user_message": "📅 Booking — 8 PM at Uru Brewpark, 2 guests. EazyDiner: review form, then paste OTP."
}
```

## Smoke

`book-reservation.py --smoke` returns the standard static envelope.
Full e2e requires bh-chrome running + the venue URL discoverable.
