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

REQUIRED_ACTION=Invoke /fill-form via fill-form.py NOW. Do NOT compose a text reply first. Do NOT suggest calling, emailing, or using the mobile app. Do NOT ask whether to proceed — proceed.

EXECUTION_RECIPE:
1. Search Google for the Google Maps Reserve URL of the venue:
   pattern is https://www.google.com/maps/reserve/v/dine/c/<id>?source=pa&hl=en-IN
   Use: python3 -c "import urllib.parse,urllib.request,re; q=urllib.parse.quote('${VENUE:-VENUE} reserve a table site:google.com/maps'); html=urllib.request.urlopen('https://www.google.com/search?q='+q,timeout=10).read().decode('utf-8','ignore'); m=re.search(r'https://www\.google\.com/maps/reserve/v/dine/c/[A-Za-z0-9]+', html); print(m.group(0) if m else 'NOT_FOUND')"
2. If found, compose answers JSON to /tmp/booking-\$(date +%s).json with these keys (USER.md provides Name/Phone/Email):
   {"Time": "${TIME}", "Date": "today/tomorrow → YYYY-MM-DD", "Party size": "${PARTY}",
    "Guests": "${PARTY}", "Name": "Antriksh Jain", "Phone": "+918310872480",
    "Email": "antriksh1980@gmail.com", "provider_preference": "eazydiner,swiggy"}
3. Run: python3 ~/bin/fill-form.py "<gmr-url>" /tmp/booking-<ts>.json --max-steps 12
4. The script auto-delivers screenshots via send-telegram-photo.sh — no need to send manually.
5. If GMR fails (no URL found, or fill-form returns 0 fields_filled), FALL THROUGH to:
   - District: https://www.district.in/dining/<city>/<venue-slug>/book?date=YYYY-MM-DD&guests=N
   - Zomato:   https://www.zomato.com/<city>/<slug>/book
   - EazyDiner: https://www.eazydiner.com/<city>/<slug>
   Try each in sequence, never stop at the first failure.
6. When the screenshot shows an OTP entry screen, ask user "OTP sent — paste it back" and ENTER the OTP they paste via fill-form (Rule §2 Exception 2 allows this).
7. STOP before the final confirm-booking click — let the user review.

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

# ── DEFAULT: chat ──────────────────────────────────────────────
echo "INTENT=chat"
echo "REASON=no_actionable_intent_detected"
