---
name: book-anything
version: 3
script: infra/book-anything.py
mutating: true
mutates_external_state: true
cron: null
eval: skills/book-anything/eval.yaml
last_audited: 2026-05-13
description: |
  End-to-end booking driver. Handles RESTAURANT reservations (Google
  Maps Reserve + provider fallbacks — the proven 7-step canonical
  flow), and ANY user-pasted booking URL for movies / theatre /
  events / generic booking forms via a modality-aware short-circuit.

  Architecture: an LLM-first intent extractor (Kimi K2.6 with
  OpenAI/Anthropic fallback) converts the user's natural-language
  message into a typed BookingIntent (modality + per-modality fields).
  The orchestrator routes by modality: restaurant → existing GMR
  platform list; movie/event/generic_url with a platform_url → drives
  the pasted URL directly via fill-form.py with the intent fields
  mapped to form answers.

  Backward-compatible: the proven Pump House GMR 7-step flow is
  unchanged — only the parser in front of it changed.

  WHEN: "book at X" / "reserve at X" / "make a reservation" /
  "book a table at X" / "book 2 tickets at <bookmyshow URL>" /
  "book this <theatre URL>" / any (book|reserve) verb +
  (venue | URL | party | time).
  NOT WHEN: User is asking ABOUT the venue ("is Uru open?" /
  "what's playing tonight"). NOT WHEN there's no venue / URL named —
  the extractor returns modality=unclear and the orchestrator asks.
trigger: |
  - "book a table at <X>" / "book at <X>" / "book <X> for <N>"
  - "reserve <X> for <N> people tonight at <T>"
  - "book me 2 tickets for <movie> tonight at <theatre>"
  - "<bookmyshow URL>" / "<district event URL>" / "<paytm insider URL>" / "book this <URL>"
  - "make a reservation at <X>" / "table for <N> at <X>"
  - resume continuation: "OTP is <digits>" / "<6 digits>" / "confirm" / "cancel"
writes_to: [/tmp/booking-state-*.json, /tmp/booking-intent-*.json]
brain_dirs_read: []
trigger_guard: any_text
---

# /book-anything

End-to-end booking driver across **all** modalities — restaurant tables, movie tickets, theatre/event tickets, and generic booking URLs the user pastes. The agent invokes this script with the user's full message; the script handles intent extraction, URL discovery (for restaurants) or direct URL drive (for pasted URLs), screenshot delivery, OTP/payment relay, and confirmation detection.

## Architecture

```
User message (free-form NL)
      │
      ▼
┌─────────────────────────────────────────────────┐
│ booking_intent.extract()                        │
│ Provider chain:                                 │
│   1. Kimi K2.6 (Moonshot OpenAI-compat)         │
│   2. OpenAI gpt-5.4-mini → Anthropic Haiku 4.5  │
│   3. Regex fallback (legacy parse_intent)       │
│ Returns: typed BookingIntent                    │
│   { modality, venue, party, datetime,           │
│     platform_url, movie_title, theatre,         │
│     ticket_count, ticket_tier, confidence,      │
│     ambiguity_notes, ... }                      │
└─────────────────────────────────────────────────┘
      │
      ▼
   Branch on modality:
     restaurant → GMR + provider list (existing 7-step)
     movie / event / generic_url + platform_url → drives the URL directly via fill-form
     unclear + confidence<0.5 → needs_clarification
```

The restaurant path is the **proven Pump House flow** — Date dropdown → Time dropdown → slot grid → Continue → provider modal → contact (First+Surname) → Book → confirmation_detected. Zero behavior change.

The generic_url path is **new in Phase 1**: when the user pastes a booking URL (BookMyShow, District events, Paytm Insider, Townscript, even a Google Form), the orchestrator skips the platform list and drives the pasted URL via fill-form.py with the intent fields mapped to form answers.

## THE 7-STEP CANONICAL FLOW (Google Maps Reserve, restaurant modality)

| Step | Action | Verification |
|---|---|---|
| 1 | Search → click **"Reserve a table"** | URL matches `google.com/maps/reserve/v/dine/c/<id>` |
| 2 | GMR widget loads with defaults | Party / Date / Time triggers visible |
| 3 | **Click Date dropdown → calendar → target day** | Date trigger reflects target |
| 4 | **Click Time dropdown → option list → target time** | Time trigger reflects target |
| 5 | Slot grid auto-refreshes, target highlighted, click Continue | Provider modal appears |
| 5b | Pick preferred provider (default: EazyDiner, fallback Swiggy) | Reservation review page appears |
| 6 | Fill First name + Surname + Phone + Email | All 4 fields read back correctly |
| 7 | Scroll to bottom → click **Book** | "Reservation confirmed" heading + green check |

### Hard rules (do not violate)

1. **NEVER click a slot-grid button before the Time dropdown shows the target time** — the grid is only ±90 min around the dropdown value.
2. **NEVER treat a listbox `<option>` as a slot button** — excluded in TIME_SLOT_CLICK_JS.
3. **NEVER click page-level Continue while the provider modal is visible** — yields to the provider-pick handler.
4. **Always split contact into First name + Surname** — many provider forms require Surname as a separate field.
5. **Success is `confirmation_detected: true`** — not `submitted: true`. The submit click can succeed while the provider rejects the booking; the confirmation page is the only ground truth.
6. **On `target_time_unavailable`, surface alternatives — never silently book the closest slot.**

## Multi-modality routing

| Modality | Trigger | What runs |
|---|---|---|
| `restaurant` | venue + party + time, no pasted URL | GMR + provider platform list (legacy 7-step) |
| `restaurant` with pinned URL | GMR/Zomato/District restaurant URL pasted | Pinned URL only, restaurant drive |
| `movie` + platform_url | BookMyShow URL or "book tickets for <movie>" with URL | generic_url branch |
| `event` + platform_url | District event / Paytm Insider / Townscript URL | generic_url branch |
| `generic_url` | "book this <any URL>" without modality hints | generic_url branch |
| `unclear` (confidence < 0.5) | Missing venue + party + time | `needs_clarification` — bot asks specifically what's missing |

### Generic URL branch behavior

When the orchestrator routes to `_drive_generic_url()`:

1. Maps intent fields → answers dict for fill-form (party → "Party size", "Guests", "Number of people"; ticket_count → "Tickets", "Quantity"; ticket_tier → "Category"; etc.)
2. Invokes `fill-form.py` on the pasted URL with `--max-steps 20`
3. Stops **before payment** (`payment_handling: stop_before` is the v1 default)
4. Returns `confirm_ready` state with a screenshot for user review
5. User replies "confirm" → re-runs with `--submit`, polls for `confirmation_detected`

**Payment relay (Phase 2):** for tickets that require payment, the user can paste card / UPI details in chat (same pattern as OTP relay per AGENTS.md §2 Exception 2). v1 always stops before payment so this can ship safely.

## Agent script of record

1. Identify booking intent (verbs: book, reserve, make a reservation, table for N, tickets for, book this URL).
2. Run:
   ```bash
   python3 ~/bin/book-anything.py "<exact user message verbatim>"
   ```
3. Screenshots auto-deliver via send-telegram-photo.sh. Relay the envelope's `user_message` field **verbatim**.
4. If `state=otp_pending`, the next user message is the OTP:
   ```bash
   python3 ~/bin/book-anything.py --resume "<otp digits>"
   ```
5. If `state=confirm_ready` and user says "confirm":
   ```bash
   python3 ~/bin/book-anything.py --submit
   ```

### Banned bail-out responses

Same anti-hallucination rules as the original /book-reservation skill. Never first-respond with "call directly", "fastest way is phone", "use the mobile app", or any text-only reply that skips invoking the script.

## Backward compatibility

The old skill name `/book-reservation` and the old script path `~/bin/book-reservation.py` resolve to this skill via thin shims for one release. AGENTS.md routing primary key is now `/book-anything`. Old triggers (book a table at X) route to the same code via the unchanged restaurant branch.

## Output envelope

```json
{
  "ok": true,
  "state": "completed | otp_pending | confirm_ready | needs_clarification | needs_time_choice | failed | all_platforms_failed",
  "platform": "gmr | district | zomato | eazydiner | swiggy | own_site | url_movie | url_event | url_generic_url",
  "intent": { ... legacy compat fields ... },
  "intent_source": "kimi | fallback_llm | regex",
  "confirmation_detected": true | false,
  "screenshot": "/tmp/...",
  "state_file": "/tmp/booking-state-<ts>.json",
  "user_message": "✅ ... | 📅 ... | ❌ ..."
}
```

`state=completed` is **only** emitted when `confirmation_detected=true`. Anything else surfaces as `confirm_ready` or `failed` with a specific message — no silent guessing.

## Smoke

```bash
python3 ~/bin/book-anything.py --smoke   # static envelope
python3 ~/bin/booking_intent.py --smoke  # extractor cases
```
