---
name: book-reservation
version: 2
script: infra/book-reservation.py
mutating: true
mutates_external_state: true
cron: null
eval: skills/book-reservation/eval.yaml
last_audited: 2026-05-13
description: |
  End-to-end restaurant / hotel / event reservation. Parses venue +
  time + party from the user message, finds the booking URL
  (Google Maps Reserve first, then Zomato → District → EazyDiner →
  Swiggy → restaurant's own site), drives the booking widget via
  fill-form.py along the **7-step canonical human flow** (see below),
  sends Telegram screenshots at each major step, stops before the
  final confirm click for user review, and waits for the user to
  paste an OTP when an OTP screen is reached. Owned by this skill —
  AGENTS.md routes all booking intents here so the bot never bails
  with "call directly".
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

## THE 7-STEP CANONICAL HUMAN FLOW (Google Maps Reserve)

This is the exact path a human takes — derived from a real working
flow we observed at Pump House, JP Nagar. Every booking driver MUST
follow this order. Deviating from this order is why bookings have
failed in the past.

| Step | Action | Verification | Failure mode if skipped |
|---|---|---|---|
| **1** | Search Google for `<venue> <area>` → click **"Reserve a table"** on the knowledge panel | URL now matches `google.com/maps/reserve/v/dine/c/<id>` | Lands on Order-online / phone path instead of GMR |
| **2** | GMR widget loads with default Party / Date / Time + a small slot grid (±90 min around default Time) | `Party`, `Date`, `Time` triggers visible | None — defaults visible immediately |
| **3** | **Click Date dropdown → calendar opens → click target day** | Date trigger text now reads target date (e.g. `Sun, 17 May`) | Slot grid stays on default day → booking on wrong date |
| **4** | **Click Time dropdown → vertical option list opens → click target time** | Time trigger text now reads target time (e.g. `9:00 pm`) | Slot grid stays in ±90min around default Time → target slot literally not in DOM |
| **5** | Slot grid auto-refreshes with target time highlighted. Click big blue **Continue** at page bottom | A "Continue with …" provider modal appears | Otherwise still on slot page |
| **5b** | In provider modal pick preferred provider (default: **EazyDiner**, fallback **Swiggy**) | URL navigates / page changes to "Your reservation" | Modal stuck → retry-flip-provider escape |
| **6** | Reservation review page shows Party / Date / Time / Contact Info / Rules / Marketing prefs. If contact missing Surname, click Contact row → fill **First name** + **Surname** + Phone + Email → click **Update info** | All four contact fields read back correctly | "Surname required" validation on Book click |
| **7** | Scroll to page bottom → click **Book** button | Confirmation page appears: heading `Reservation confirmed`, green check, "Email sent to …" | Without scroll the Book button may be below viewport on some widgets |
| **✓** | Detect `Reservation confirmed` heading + party/date/time on success page | `confirmation_detected: true` in envelope | Without this signal the orchestrator can't distinguish success from "submit clicked but page is still loading" |

### Hard rules derived from the flow

1. **NEVER click a slot-grid button before the Time dropdown shows the target time.** The grid is only ±90min around the dropdown's current value; the target may literally not be in the DOM. Strict match (±5 min) only.
2. **NEVER treat a listbox `<option>` (from the open Date/Time dropdown) as a slot button.** Listbox options live inside `[role=listbox]` and visually look like slots — they are not. (Excluded in `TIME_SLOT_CLICK_JS`.)
3. **NEVER click the page-level Continue while the "Continue with …" provider modal is visible.** Yields to the provider-pick handler. (Enforced in `CTA_CLICK_JS`.)
4. **Always split contact into `First name` + `Surname`** in `USER_DEFAULTS`. Many provider forms validate `Surname` as required; a single `Name` field cross-matches the wrong way.
5. **Success is `confirmation_detected: true`** — not `submitted: true`. The submit click can succeed while the booking is later rejected; the confirmation page is the only ground truth.
6. **On `target_time_unavailable`, surface alternatives — never silently book the closest slot.** This is what caused the wrong-day booking earlier.

## Agent script of record

1. Identify booking intent in the user's message (verbs: book, reserve, make a reservation, table for N, etc.).
2. Write the full user message verbatim to `/tmp/book-msg-<ts>.txt`.
3. Run:
   ```bash
   python3 ~/bin/book-reservation.py "$(cat /tmp/book-msg-<ts>.txt)"
   ```
4. The script auto-delivers screenshots via send-telegram-photo.sh throughout the flow. You do NOT need to relay them manually.
5. Relay the envelope's `user_message` field **verbatim** as your reply. Do not invent success / failure language — the script knows what state it's in.

### OTP / resume turns

If the script's prior turn returned `state=otp_pending`, the next user message is likely the OTP. Pass it as:
```bash
python3 ~/bin/book-reservation.py --resume "$(cat /tmp/last-otp.txt)"
```

### Confirm turns

If the script's prior turn returned `state=confirm_ready` and the user replies "confirm", invoke:
```bash
python3 ~/bin/book-reservation.py --submit
```
The script re-opens the URL, walks through steps 6-7, clicks Book, and waits for `Reservation confirmed`. Only when `state=completed` AND `confirmation_detected=true` should the bot tell the user the booking went through.

## Banned bail-out responses

NEVER respond with these as a first turn to a booking intent:

- "Call +91 ... directly — fastest way"
- "Calling is the most reliable path"
- "Given the time-sensitive nature, your fastest option is to call"
- "Web booking is blocked / requires app login" (without running the script first)
- "District: no booking available for this venue" (without trying)
- Any text reply that doesn't include a book-reservation.py invocation

If you respond with text-only to a booking-INTENT message you have FAILED the task. The user explicitly chose automated booking; the phone path is a LAST resort the script surfaces ONLY after all platforms have produced documented failures.

## Platform priority

Tried in this order, falling through ONLY on documented failure (0 fields filled OR explicit error from fill-form):

| # | Platform | URL pattern | Notes |
|---|---|---|---|
| 1 | **Google Maps Reserve** | `https://www.google.com/maps/reserve/v/dine/c/<id>?source=pa&hl=en-IN` | **ALWAYS first.** Aggregator that surfaces EazyDiner / Swiggy / OpenTable times. Free for the diner (no cover charge). Drives the 7-step flow above. |
| 2 | **District by Zomato** | `https://www.district.in/dining/<city>/<slug>/book?date=<YYYY-MM-DD>&guests=<N>` | Often has cover charges — only used if GMR fails. |
| 3 | **Zomato direct** | `https://www.zomato.com/<city>/<slug>/book` | |
| 4 | **EazyDiner** | `https://www.eazydiner.com/<city>/<slug>` | Same backend as GMR's EazyDiner provider. |
| 5 | **Swiggy Dineout** | search → first swiggy.com/dineout result | |
| 6 | **Restaurant's own site** | search → main domain → look for Reserve / Book CTA | |
| 7 | **Phone / email** | LAST resort, only after #1-6 documented failures | Bot quotes the venue's phone + asks user to call. |

URL discovery is via Google search inside the script (no MCP needed). The script reuses fill-form.py for all actual form drives — this skill is the orchestrator, fill-form is the executor.

## OTP handling

Permitted per AGENTS.md §2 Exception 2 — user-originated OTPs in chat are treated as form answers. When the booking screenshot shows an OTP entry screen:

1. Script saves state to `/tmp/booking-state-<ts>.json` with `state=otp_pending`, the URL, the OTP field selector hint, and remaining answers.
2. Script sends Telegram message: "OTP screen reached — paste the code you received".
3. Script EXITS (turn ends).
4. When user replies with the OTP, the agent calls `book-reservation.py --resume "<otp digits>"` which loads the most recent state file and continues the fill via fill-form.

## Output envelope

Single-line JSON on stdout. Key fields:

```json
{
  "ok": true,
  "state": "completed | otp_pending | confirm_ready | needs_time_choice | failed | all_platforms_failed",
  "platform": "gmr | district | zomato | eazydiner | swiggy | own_site | phone",
  "venue": "Uru Brewpark",
  "party": 2,
  "time": "9:00 PM",
  "date": "2026-05-17",
  "url_tried": "https://...",
  "confirmation_detected": true,
  "screenshots": ["/tmp/booking-step-0.png", ...],
  "state_file": "/tmp/booking-state-<ts>.json",
  "user_message": "✅ Reservation confirmed at Uru Brewpark — 9 PM on 17 May, party of 2. Email sent."
}
```

`state=completed` is only emitted when `confirmation_detected=true`. Anything else surfaces as `confirm_ready` (waiting on user) or `failed` (surfaces a clear reason).

## Smoke

`book-reservation.py --smoke` returns the standard static envelope. Full e2e requires bh-chrome running + the venue URL discoverable.
