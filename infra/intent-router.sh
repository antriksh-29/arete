#!/bin/bash
# intent-router.sh — deterministic first-pass intent classifier.
# The main agent MUST call this as a tool BEFORE composing any reply.
# stdout is a structured directive that the agent must follow verbatim
# for actionable intents (booking, form-fill, etc.). For chat-only
# intents it returns INTENT=chat and the agent proceeds normally.
#
# Why this exists: AGENTS.md prose rules ("you MUST run /fill-form
# first") are inconsistently followed by gpt-5.4-mini. Tool output is
# followed more reliably. This shell wraps the routing decision in a
# tool call, removing LLM judgment from the dispatch.
#
# Usage: intent-router.sh "<user message verbatim>"

set -u  # not -e — grep returning 1 on no-match is expected

MSG="${1:-}"
if [ -z "$MSG" ]; then
  echo "INTENT=chat"
  echo "REASON=empty_message"
  exit 0
fi

# Lowercased version for matching
LMSG=$(echo "$MSG" | tr '[:upper:]' '[:lower:]')

# ── BOOKING INTENT ─────────────────────────────────────────────
# Match patterns: book/reserve/reservation + (table|restaurant|dinner|
# lunch|brunch|venue|hotel|room) OR explicit venue names.
BOOKING_MATCH=0
if echo "$LMSG" | grep -qE '\b(book|reserve|reservation|booking)\b'; then
  if echo "$LMSG" | grep -qE '\b(table|restaurant|dinner|lunch|brunch|venue|hotel|room|stay|brewpub|brewery|bar|pub|cafe|cafetería|restro|eatery|dining|kitchen|grill|bistro|appointment)\b'; then
    BOOKING_MATCH=1
  fi
  # Also match "book X for Y people" / "book for Y at Z"
  if echo "$LMSG" | grep -qE '\bfor\s+[0-9]+\b'; then
    BOOKING_MATCH=1
  fi
  # Also match "book ... at <CapWord>" (named venue)
  if echo "$LMSG" | grep -qE '\b(at|@)\s+[A-Z]'; then
    BOOKING_MATCH=1
  fi
fi

if [ "$BOOKING_MATCH" -eq 1 ]; then
  # Parse rough venue (best-effort)
  VENUE=$(echo "$MSG" | grep -oiE '(at|@)\s+[A-Z][A-Za-z0-9 &\x27.-]+' | head -1 | sed -E 's/^[[:space:]]*(at|@)[[:space:]]+//I' | sed -E 's/[[:space:]]+(in|at|near|for|tonight|tomorrow|today|on|this|next)[[:space:]]+.*$//I' | head -c 100)
  # Parse party size
  PARTY=$(echo "$LMSG" | grep -oE '\b[0-9]+\s+(people|guests|persons|of us|adults)\b' | head -1 | grep -oE '^[0-9]+')
  if [ -z "$PARTY" ]; then
    PARTY=$(echo "$LMSG" | grep -oE '\bfor\s+[0-9]+\b' | head -1 | grep -oE '[0-9]+')
  fi
  # Parse time
  TIME=$(echo "$MSG" | grep -oiE '\b[0-9]{1,2}(:[0-9]{2})?\s*(am|pm|AM|PM)\b' | head -1)
  if [ -z "$TIME" ]; then
    TIME=$(echo "$LMSG" | grep -oE '\b(tonight|tomorrow|today|noon|midnight)\b' | head -1)
  fi
  # Parse day reference
  DAY=$(echo "$LMSG" | grep -oE '\b(tonight|tomorrow|today|this evening|tomorrow evening|tomorrow night)\b' | head -1)

  cat <<EOF
INTENT=booking
VENUE=${VENUE:-UNPARSED — ask user}
PARTY=${PARTY:-UNPARSED — ask user}
TIME=${TIME:-UNPARSED — ask user}
DAY=${DAY:-UNPARSED — assume today}

REQUIRED_ACTION=Invoke /book-reservation NOW. The skill handles parsing, URL discovery, platform fallback (GMR → District → Zomato → EazyDiner → Swiggy), screenshots, and OTP relay. Do NOT compose a text reply first. Do NOT suggest calling, emailing, or using the mobile app.

EXECUTION_RECIPE:
1. Run: python3 ~/bin/book-reservation.py "<the user's exact message verbatim>"
2. The script auto-delivers screenshots via send-telegram-photo.sh throughout the flow.
3. Relay the envelope's user_message field VERBATIM as your reply.
4. If state=otp_pending in the envelope, the user's next message will be the OTP. Pass it as: python3 ~/bin/book-reservation.py --resume "<otp digits>"
5. The script STOPS before the final confirm-booking click — the user reviews the screenshot then replies 'confirm' to submit.

BANNED_RESPONSES:
- "The fastest route is to call directly"
- "Calling is the fastest and most reliable way"
- "Given the time-sensitive nature, your fastest option is to call/email"
- "Would you like me to try X as a backup option?"
- "District: no booking available for this venue" (without first running fill-form on District URL)
- ANY text-only reply that doesn't include a fill-form tool invocation

If you respond with text-only to a booking request, you have FAILED the task. The user explicitly demanded automated booking via the platform chain — see identity/SOUL.md "What to avoid" → bail-out responses.
EOF
  exit 0
fi

# ── OTP RELAY INTENT ───────────────────────────────────────────
# User pastes "OTP is 123456" or "code: 123456" in chat — the agent
# must type it into the OTP field via fill-form, NOT refuse.
OTP_CODE=$(echo "$MSG" | grep -oE '\b[0-9]{4,8}\b' | head -1)
if [ -n "$OTP_CODE" ] && echo "$LMSG" | grep -qE '\b(otp|code|verification|verify|2fa|pin)\b'; then
  cat <<EOF
INTENT=otp_relay
OTP=${OTP_CODE}

REQUIRED_ACTION=Type the OTP into the verification field via fill-form.py with answer key "OTP" or "Verification code" set to "${OTP_CODE}". Refer to identity/AGENTS.md §2 Exception 2 — user-provided OTPs are a MUST, not a MAY.

BANNED_RESPONSES:
- "I can't intercept your OTP"
- "OTPs need to be entered on your device"
- "The security model is..."
- Any refusal to use the user-provided code.
EOF
  exit 0
fi

# ── BOOKING TIME-PICK ──────────────────────────────────────────
# After a needs_time_choice envelope, the user replies with just a
# time ("10 PM", "9:30pm", "10:00"). Detect when the message is
# *mostly* just a time and route to book-reservation.py --pick-time.
TRIM=$(echo "$MSG" | xargs)
if [ ${#TRIM} -lt 25 ]; then
  if echo "$TRIM" | grep -qiE '^[0-9]{1,2}(:[0-9]{2})?\s*(am|pm)?$'; then
    cat <<EOF
INTENT=booking_pick_time
TIME=${TRIM}

REQUIRED_ACTION=Run: python3 ~/bin/book-reservation.py --pick-time "${TRIM}"
Relay the envelope's user_message verbatim. The script loads the
most recent needs_time_choice state and re-drives the same platform
with the new time.

BANNED_RESPONSES:
- Any reply that doesn't include the book-reservation.py --pick-time invocation when there's a pending needs_time_choice state.
EOF
    exit 0
  fi
fi

# ── DEFAULT: chat ──────────────────────────────────────────────
echo "INTENT=chat"
echo "REASON=no_actionable_intent_detected"
