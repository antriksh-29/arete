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

# ── BOOKING CONFIRM ────────────────────────────────────────────
# After state=confirm_ready, the user replies "confirm" / "yes book" /
# "go ahead" / "please book" — route to book-reservation.py --submit
# so the script actually clicks the platform's final Continue/Submit.
if echo "$LMSG" | grep -qE '^[[:space:]]*(confirm|yes\b|go ahead|book it|please book|submit it|do it)( - please book| please book| and book)?[[:space:]]*\.?\??!?[[:space:]]*$'; then
  cat <<EOF
INTENT=booking_confirm

REQUIRED_ACTION=Run: python3 ~/bin/book-reservation.py --submit
Relay the envelope's user_message verbatim. The script loads the
most recent confirm_ready/needs_time_choice state and clicks the
final Continue/Submit on the platform.

BANNED_RESPONSES:
- Any reply that says the booking "got cut off" without a fresh --submit attempt.
- Acknowledgements like "I'll submit now" without actually running the script.
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

# ── DAILY-TASK INTENTS ─────────────────────────────────────────
# Six sub-intents for the daily-task-manager skill. Each routes to a
# specific ~/bin/daily-tasks.sh subcommand. Kimi K2.6 reliably emits
# tool calls when given a deterministic dispatch table; relying on
# SKILL.md prose alone gets chain-of-thought leaks instead of tool
# invocations.

# task_review — numbered status reply (the 22:00 prompt response).
# Distinguishing signal: at least 2 lines that start with N. / N) / N -
# followed by a status word.
TASK_REVIEW_LINES=$(echo "$MSG" | grep -cE '^[[:space:]]*[0-9]+[[:space:].):-]+[a-z]')
if [ "$TASK_REVIEW_LINES" -ge 2 ] && echo "$LMSG" | grep -qE '\b(done|tomorrow|later|skip|push|defer|park|complete)\b'; then
  # Try to extract the prompt date from a recent "📋 Daily task review
  # for YYYY-MM-DD" message — but the router can't see chat history,
  # so default to today and let the agent override --date if needed.
  cat <<EOF
INTENT=task_review

REQUIRED_ACTION=Apply the numbered status reply via the daily-tasks script. ALWAYS use --reply arg form, NOT stdin (model failover can drop stdin → silent zero-update — the May-13 outage).

EXECUTION_RECIPE:
1. Find the most recent "📋 Daily task review for <YYYY-MM-DD>" message in chat (the 22:00 prompt) and extract <YYYY-MM-DD>. If not visible, use today.
2. Run: ~/bin/daily-tasks.sh review --date <YYYY-MM-DD> --reply "<the user's exact reply verbatim>"
3. Relay the script's stdout VERBATIM. If exit != 0, relay the stderr too.

BANNED_RESPONSES:
- "Applied 0 status updates" passed through without re-running with the right --reply payload.
- Any claim of saving statuses without invoking the script.
EOF
  exit 0
fi

# task_set — sharing a list of 2+ tasks framed as today's work.
if echo "$LMSG" | grep -qE '\b(my tasks today|todays tasks|today.s tasks|tasks for today|to.?do for today|tasks i.ll do today|let me share my tasks|here are my tasks|tasks i need to do today)\b'; then
  cat <<EOF
INTENT=task_set

REQUIRED_ACTION=Persist the task list via the daily-tasks script.

EXECUTION_RECIPE:
1. Run: ~/bin/daily-tasks.sh set <<'EOF_TASKS'
<task 1, one per line, EXACTLY as the user phrased them>
<task 2>
...
EOF_TASKS
2. Relay the script's one-line ack VERBATIM (e.g. "✓ Stored N tasks for YYYY-MM-DD").

BANNED_RESPONSES:
- Fabricated "Saved" / "Got it, N tasks logged" replies without running the script (the May-13 hallucination shape).
EOF
  exit 0
fi

# task_add — appending to today's list.
if echo "$LMSG" | grep -qE '\b(add (it )?to my (list|tasks)|also add|append to my list|add this to today)\b'; then
  cat <<EOF
INTENT=task_add

EXECUTION_RECIPE:
1. Run: ~/bin/daily-tasks.sh add <<'EOF_TASKS'
<the task title verbatim>
EOF_TASKS
2. Relay the script's one-line ack verbatim.
EOF
  exit 0
fi

# task_mark — single-task status mutation.
if echo "$LMSG" | grep -qE '\b(i finished|done with|finished #|complete[d]? #|push #[0-9]+|defer #[0-9]+|park #[0-9]+|mark #?[0-9]+|remove #?[0-9]+|drop the [a-z]+ one|delete from my list)\b'; then
  cat <<EOF
INTENT=task_mark

EXECUTION_RECIPE:
Pick the right subcommand based on the user's intent:
- "I finished #N" / "done with X" → ~/bin/daily-tasks.sh mark <N|substring> yes
- "push #N to tomorrow" / "defer #N" → ~/bin/daily-tasks.sh mark <N> tomorrow
- "park #N indefinitely" / "later" → ~/bin/daily-tasks.sh mark <N> later
- "remove #N" / "delete X" / "drop the third one" → ~/bin/daily-tasks.sh remove <N|substring>
Relay the script's one-line ack verbatim.
EOF
  exit 0
fi

# task_backlog — open tasks aged ≥7 days.
if echo "$LMSG" | grep -qE '\b(my backlog|backlog of tasks|what.s pending|whats pending|pending from my end|stale tasks|stuck tasks|open tasks|tasks still pending|what.s open|whats open|aged tasks)\b'; then
  # Optional --age-gte flag if user specified "for N days/weeks"
  AGE=""
  if echo "$LMSG" | grep -qE '\b(2|two)\s+week'; then AGE=" --age-gte 14"; fi
  if echo "$LMSG" | grep -qE '\b(3|three)\s+week'; then AGE=" --age-gte 21"; fi
  if echo "$LMSG" | grep -qE '\b(month|30\s+day)'; then AGE=" --age-gte 30"; fi
  cat <<EOF
INTENT=task_backlog

REQUIRED_ACTION=Show the daily-tasks backlog via the canonical script. DO NOT compose this from brain pages or chat memory — the backlog is computed live from per-task lineage.

EXECUTION_RECIPE:
1. Run: ~/bin/daily-tasks.sh backlog${AGE}
2. Relay the script's stdout VERBATIM. Starts with "📋 Backlog —" and lists tasks with "(Xd)" age tags.

BANNED_RESPONSES:
- Replies that pull from goals/projects/originals pages.
- "Personal goals" or "Business" categorized lists that aren't from the script.
- Hallucinated counts or paraphrased backlog entries.
EOF
  exit 0
fi

# task_stats — completion percentage / push count over a window.
if echo "$LMSG" | grep -qE '\b(task stats|completion rate|how many (tasks|did i (close|finish|complete))|tasks (closed|done) (this|last)\s+(week|month)|task percentage)\b'; then
  WIN="week"
  if echo "$LMSG" | grep -qE '\bmonth\b'; then WIN="month"; fi
  if echo "$LMSG" | grep -qE '\b(all.time|overall|total|ever)\b'; then WIN="all"; fi
  cat <<EOF
INTENT=task_stats
WINDOW=${WIN}

EXECUTION_RECIPE:
1. Run: ~/bin/daily-tasks.sh stats --window ${WIN}
2. Relay the script's stdout VERBATIM. Starts with "📊 Stats —".

BANNED_RESPONSES:
- Counting tasks from memory ("I already counted N earlier...").
- Reading habits/daily-tasks-stats.md and paraphrasing — always re-run the script.
EOF
  exit 0
fi

# task_history — date-span archive query.
HIST_RANGE=$(echo "$LMSG" | grep -oE '\b[0-9]{4}-[0-9]{2}-[0-9]{2}\s*(to|through|until|\.\.|-)\s*[0-9]{4}-[0-9]{2}-[0-9]{2}\b' | head -1)
if [ -n "$HIST_RANGE" ] || echo "$LMSG" | grep -qE '\b(tasks from (last|past)|task history|task archive|tasks between|tasks for the (last|past))\b'; then
  cat <<EOF
INTENT=task_history

EXECUTION_RECIPE:
1. Parse the date range. If explicit (YYYY-MM-DD..YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD), use it verbatim.
   Otherwise compute: "last week" = today-7..today, "last month" = today-30..today.
2. Run: ~/bin/daily-tasks.sh history <start>..<end>
3. Relay the script's stdout verbatim.
EOF
  exit 0
fi

# task_show — request to see today/yesterday/specific-date tasks.
# Liberal matching: plurals optional, apostrophes optional (Telegram
# strips them), "what is" alongside "what's".
TASK_SHOW=0
# Pattern A: "show <date-ish> task(s|list)" with optional apostrophe-s.
#   - show today's tasks / show today task list / show yesterdays tasks /
#     show tasks for 2026-05-13 / show my tasks 3 days ago
if echo "$LMSG" | grep -qE '\bshow\b.*\btask' && \
   echo "$LMSG" | grep -qE '\b(today|todays|yesterday|yesterdays|[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]+\s+days?\s+ago)\b'; then
  TASK_SHOW=1
fi
# Pattern B: standalone phrasings.
if echo "$LMSG" | grep -qE '\b(share (today|todays).*task|what.?(s| is) on my plate|what am i working on|my (current )?tasks|today.?s task list|todays task list|tasks for (today|yesterday)|tasks for (may|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)|tasks [0-9]+ days? ago)\b'; then
  TASK_SHOW=1
fi
if [ "$TASK_SHOW" -eq 1 ]; then
  # Resolve target date (today | yesterday | YYYY-MM-DD | -N)
  TARGET=""
  if echo "$LMSG" | grep -qE '\byesterday\b'; then TARGET="yesterday"; fi
  YMD=$(echo "$LMSG" | grep -oE '\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b' | head -1)
  if [ -n "$YMD" ]; then TARGET="$YMD"; fi
  DAYS_AGO=$(echo "$LMSG" | grep -oE '\b[0-9]+\s+days?\s+ago\b' | head -1 | grep -oE '^[0-9]+')
  if [ -n "$DAYS_AGO" ]; then TARGET="-$DAYS_AGO"; fi
  cat <<EOF
INTENT=task_show
TARGET=${TARGET:-today}

EXECUTION_RECIPE:
1. Run: ~/bin/daily-tasks.sh show ${TARGET:-today}
2. Relay the script's stdout VERBATIM. Header is "📋 <Today|Yesterday|YYYY-MM-DD> — N tasks". Status emojis (✅ ➡️ ⏸️ 🗑️ •) are MANDATORY — they come from the script. Do NOT add or remove them.

BANNED_RESPONSES:
- Adding "— pending" or other suffixes the script doesn't produce.
- Categorizing into "Work" / "Personal" / "Errands" sections — the script lists tasks in a flat numbered list.
- Paraphrasing task titles.
EOF
  exit 0
fi

# ── WEB SEARCH INTENT ──────────────────────────────────────────
# Match live-info queries the model otherwise refuses ("no web access").
# Narrow patterns so we don't capture chat / brain questions.
WEB_SEARCH=0
if echo "$LMSG" | grep -qE '\b(search the web|web[- ]search|search online)\b'; then WEB_SEARCH=1; fi
if echo "$LMSG" | grep -qE '\bnews (on|about|for|in)\b'; then WEB_SEARCH=1; fi
if echo "$LMSG" | grep -qE "\bwhat'?s? happening (with|in|to|on|around)\b"; then WEB_SEARCH=1; fi
if echo "$LMSG" | grep -qE '\b(latest|current)\s+\S+\s+(price|news|release|version|stats?|results?|score|reviews?)\b'; then WEB_SEARCH=1; fi
if echo "$LMSG" | grep -qE '^look up\b' || echo "$LMSG" | grep -qE '\blook up\b.*\bon the web\b'; then WEB_SEARCH=1; fi
if echo "$LMSG" | grep -qE '\bfind (me )?(a )?source for\b'; then WEB_SEARCH=1; fi
if [ "$WEB_SEARCH" -eq 1 ]; then
  cat <<EOF
INTENT=web_search

EXECUTION_RECIPE:
1. Tightly paraphrase the user's query into a short search string (strip
   filler like "can you", "search the web for", "please", "I want to know").
2. Run: bash ~/bin/web-search.sh "<paraphrased query>"
3. Relay the script's stdout VERBATIM (a numbered markdown list with
   title, snippet, and URL per result). You may add ONE sentence of
   framing before the list. Do NOT re-summarize or invent results.

BANNED_RESPONSES:
- "I don't have web access" / "no web search configured" / any
  apology that you can't search. The script always works — run it.
- Pasting URLs from your own training memory instead of the script output.
EOF
  exit 0
fi

# ── MCPORTER LIST INTENT ───────────────────────────────────────
# Narrow: only fires on explicit "list MCP" phrasings.
if echo "$LMSG" | grep -qE '\b(list|show|what)\b.*\bmcp( servers?| tools?)?\b' \
   || echo "$LMSG" | grep -qE '\bmcp servers? (configured|installed|active|available)\b' \
   || echo "$LMSG" | grep -qE '\bmcporter list\b'; then
  cat <<EOF
INTENT=mcporter_list

EXECUTION_RECIPE:
1. Run: bash ~/bin/mcporter-list.sh
2. Relay the script's stdout VERBATIM. If the output starts with "No MCP
   servers configured.", relay that — do not invent a list.

BANNED_RESPONSES:
- Suggesting the user run "openclaw status" or "cat ~/.openclaw/config.json".
- "I don't have a direct tool" — you do; run the wrapper above.
EOF
  exit 0
fi

# ── GIFGREP SEARCH INTENT ──────────────────────────────────────
# Match "find a gif of X", "show me gifs", "search Giphy/Tenor for X".
if echo "$LMSG" | grep -qE '\b(find|search|grab|get)\b.*\bgif(s)?\b' \
   || echo "$LMSG" | grep -qE '\bgif of\b' \
   || echo "$LMSG" | grep -qE '\b(show|send) (me )?(a )?gif(s)?\b' \
   || echo "$LMSG" | grep -qE '\bsearch (giphy|tenor)\b'; then
  # Best-effort query extraction. Handles "find a gif of X", "find a X gif",
  # "show me gifs of X", "search Giphy for X". If extraction misfires, the
  # full message still works as a fallback query.
  QUERY=$(echo "$MSG" \
    | sed -E 's/^(please |can you )?//I' \
    | sed -E 's/^(find|search|grab|get|show|send|fetch) (me )?(a |the |some )?(gif of |gifs of )?//I' \
    | sed -E 's/^(search )?(giphy|tenor)( for)? //I' \
    | sed -E 's/\s+(gif|gifs|\.gif)\s*$//I' \
    | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')
  [ -z "$QUERY" ] && QUERY="$MSG"
  cat <<EOF
INTENT=gifgrep_search
QUERY=$QUERY

EXECUTION_RECIPE:
1. Run: bash ~/bin/gifgrep.sh "$QUERY"
2. Relay the script's stdout VERBATIM. If output starts with "ABORT:
   KLIPY_API_KEY missing", relay that line — the user knows what to do.

BANNED_RESPONSES:
- "I don't have image search capability" — you do; run the wrapper.
- Suggesting the user check Giphy/Tenor manually.
EOF
  exit 0
fi

# ── DEFAULT: chat ──────────────────────────────────────────────
echo "INTENT=chat"
echo "REASON=no_actionable_intent_detected"
