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

# ── SKILL AUTHORING (T14 — implicit + explicit + CONTINUE) ──────
#
# Three routes (in priority order):
#   0. CONTINUE — there's an active draft from a previous turn with
#      unanswered questions. The user's current message is the next
#      answer. Route to elicit.ts --mode=ingest-answer.
#   1. Explicit /teach or /refine <name> slash command — start fresh.
#   2. Implicit — natural language signals (e.g. "I wish you would
#      always...", "the X skill should also...").
#
# D7 firewall: this script is invoked by spawn-signal-detector.sh,
# which is invoked by AGENTS.md Rule 0 — ONLY on direct Telegram user
# turns. Ingested-content paths never reach this code path (per §15).

# --- ROUTE 0: CONTINUE an active draft ---
# An active draft is a JSON file under .context/skill-author-drafts/
# with completed_questions count below the required threshold for
# its mode (create: 5 questions, refine: 3 questions). The "latest"
# active draft is the one with the most-recent updated_at timestamp.
ACTIVE_DRAFT=""
ACTIVE_DRAFT_MODE=""
DRAFTS_DIR="$HOME/personal-assistant/.context/skill-author-drafts"
if [ -d "$DRAFTS_DIR" ]; then
  # Find any draft files modified in the last 30 minutes that aren't done.
  while IFS= read -r f; do
    [ -z "$f" ] || [ ! -f "$f" ] && continue
    completed=$(jq -r '.completed_questions | length' "$f" 2>/dev/null || echo 0)
    mode=$(jq -r '.mode' "$f" 2>/dev/null || echo "")
    awaiting=$(jq -r '.awaiting_confirmation // false' "$f" 2>/dev/null)
    required=5
    [ "$mode" = "refine" ] && required=3
    # A draft is "still active" if either:
    #   (a) it's waiting for the user's yes/no/cancel after the summary, OR
    #   (b) it still has unanswered questions.
    # Note for manual-mode create: completed_questions = [1,2,3,5] (4 items)
    # but the script considers Q4 skipped, so the draft IS complete and
    # awaiting_confirmation is set. The router needs this flag, not just
    # the count, to route the user's confirmation message correctly.
    if [ "$awaiting" = "true" ] || [ "$completed" -lt "$required" ] 2>/dev/null; then
      ACTIVE_DRAFT="$(basename "$f" .json)"
      ACTIVE_DRAFT_MODE="$mode"
      break
    fi
  done < <(find "$DRAFTS_DIR" -name '*.json' -mmin -30 -printf '%T@ %p\n' 2>/dev/null \
           | sort -rn | awk '{print $2}')
fi

# If we found an active draft, ANY non-slash-command message from the
# user is the next answer. Slash commands (/teach, /refine, /cancel)
# still take precedence below.
if [ -n "$ACTIVE_DRAFT" ] && ! echo "$LMSG" | grep -qE '^[[:space:]]*/(teach|refine|cancel|abort|stop)\b'; then
  # Escape double-quotes in the message for shell safety in the recipe.
  ESCAPED_MSG=$(printf '%s' "$MSG" | sed 's/"/\\"/g')
  cat <<EOF
INTENT=skill_authoring_continue
DRAFT_SLUG=${ACTIVE_DRAFT}
DRAFT_MODE=${ACTIVE_DRAFT_MODE}
CONFIDENCE=100
TRIGGER_SOURCE=active_draft_continue

REQUIRED_ACTION=The user has an active skill-authoring draft. Their current message is the next answer. Your ONLY action this turn is to run elicit.ts in ingest-answer mode. Do NOT compose any text reply. Do NOT pre-fill any other answers. Do NOT analyze source code. Do NOT interpret the message — pass it through as-is.

EXECUTION_RECIPE:
1. Run EXACTLY this Bash command (no other tool calls):
   ~/.bun/bin/bun run ~/personal-assistant/meta/skill-author/scripts/elicit.ts --mode=ingest-answer --slug=${ACTIVE_DRAFT} --message="${ESCAPED_MSG}"
2. The script returns a single-line JSON envelope on stdout. Parse it.
3. Take the envelope's \`user_message\` field VERBATIM. Send it as your Telegram reply.
4. STOP. Do not append explanations, summaries, or interpretations.

BANNED_RESPONSES:
- ANY reply that doesn't include the elicit.ts tool call above.
- Inventing your own next question, summary, or interpretation of the user's answer.
- Pre-filling other fields you "infer" from context (e.g. assuming "daily 8am" implies a specific output format).
- Reading or analyzing the source code of elicit.ts / commit-and-deploy.ts / safety-scan.ts — the user is mid-conversation and is waiting for you to PROCESS their answer, not REVIEW your codebase.
- Skipping ahead — even if the user gave a verbose answer that "contains" answers to later questions, pass it to elicit ONCE and let the state machine decide what to ask next.
EOF
  exit 0
fi

# --- ROUTE 1a: Explicit /teach ---
if echo "$LMSG" | grep -qE '^[[:space:]]*/teach\b'; then
  cat <<EOF
INTENT=skill_create
TARGET=
CONFIDENCE=100
TRIGGER_SOURCE=explicit_slash_command

REQUIRED_ACTION=Start a brand-new skill-create elicit. Your ONLY action this turn is to run elicit.ts ONCE. Do NOT compose any text reply yourself. Do NOT pre-fill any answers. Do NOT analyze source code. The user clicked /teach so they want to start fresh — start the elicit, get Q1 back, relay it.

EXECUTION_RECIPE:
1. Run EXACTLY this Bash command (no other tool calls):
   ~/.bun/bin/bun run ~/personal-assistant/meta/skill-author/scripts/elicit.ts --mode=create
2. The script returns a single-line JSON envelope on stdout. Parse it.
3. Take the envelope's \`user_message\` field VERBATIM. Send it as your Telegram reply.
4. STOP. The remaining questions (Q2, Q3, …) come on FUTURE turns once the user answers Q1.

BANNED_RESPONSES:
- Composing your own version of Q1 instead of running elicit.ts.
- Calling elicit.ts more than ONCE per turn.
- Reading the elicit.ts source code or "explaining" the pipeline to the user.
- Pre-filling answers based on prior turns or session memory — /teach means start fresh.
- Asking multiple questions in one reply.
EOF
  exit 0
fi

# --- ROUTE 1b: Explicit /refine <name> ---
REFINE_TARGET=$(echo "$MSG" | grep -oiE '^[[:space:]]*/refine[[:space:]]+([a-z0-9_-]+)' | head -1 | awk '{print $NF}')
if [ -n "$REFINE_TARGET" ]; then
  cat <<EOF
INTENT=skill_refine
TARGET=${REFINE_TARGET}
CONFIDENCE=100
TRIGGER_SOURCE=explicit_slash_command

REQUIRED_ACTION=Start the refine elicit for skill ${REFINE_TARGET}. Your ONLY action this turn is to run elicit.ts ONCE.

EXECUTION_RECIPE:
1. Run EXACTLY this Bash command (no other tool calls):
   ~/.bun/bin/bun run ~/personal-assistant/meta/skill-author/scripts/elicit.ts --mode=refine --target=${REFINE_TARGET}
2. The script returns a single-line JSON envelope on stdout. Parse it.
3. Take the envelope's \`user_message\` field VERBATIM. Send it as your Telegram reply.
4. STOP. R2, R3 come on future turns once the user answers R1.

BANNED_RESPONSES:
- Composing your own version of R1.
- Reading the source SKILL.md and "summarizing" it instead of running elicit.
- Pre-filling answers from session memory.
EOF
  exit 0
fi

# --- Implicit create signals (natural language) ---
# "I wish you would..." / "I wish you could..."
# "make a skill for ..." / "let's make a skill ..."
# "save this as a skill" / "save that as a skill"
# "remember to ... from now on" / "from now on, ..."
# "always reply with ..." / "always include ..."
# "next time I ask for ..., do ..."
IMPLICIT_CREATE=0
if echo "$LMSG" | grep -qE '\b(i wish you (would|could) (always|just|please)?)\b'; then IMPLICIT_CREATE=1; fi
if echo "$LMSG" | grep -qE "\b(save (this|that) as a skill|make (this|that) (into )?a skill|let'?s make a skill|let'?s turn (this|that) into a skill|codify (this|that))\b"; then IMPLICIT_CREATE=1; fi
if echo "$LMSG" | grep -qE '\b(from now on|next time)\b.{0,80}\b(do|reply|send|always|include|make sure)\b'; then IMPLICIT_CREATE=1; fi
if echo "$LMSG" | grep -qE '\balways (reply|respond|include|send|do)\b'; then IMPLICIT_CREATE=1; fi
if echo "$LMSG" | grep -qE '\bremember to .{3,80}\b(every (day|morning|evening|week|hour)|when|whenever|always|next time)\b'; then IMPLICIT_CREATE=1; fi
# Recurring-on-schedule: "send/give me X every morning|day|week"
if echo "$LMSG" | grep -qE '\b(send|give|share|show|tell|remind|notify|email|message|text) me .{3,120}\b(every|daily|weekly|hourly|each)\s+(morning|evening|afternoon|night|day|week|month|hour|minute|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'; then IMPLICIT_CREATE=1; fi
# Front-loaded: "every <period>, send/give me X"
if echo "$LMSG" | grep -qE '\bevery\s+(morning|evening|day|week|month|hour|monday|tuesday|wednesday|thursday|friday|saturday|sunday|night)\b.{0,80}(send|give|share|show|tell|remind|notify|email|message)\s+me\b'; then IMPLICIT_CREATE=1; fi
# Time-anchored: "at <time>, send/give me X"
if echo "$LMSG" | grep -qE '\bat\s+[0-9]{1,2}(:[0-9]{2})?\s*(am|pm)?\b.{0,80}(send|give|share|tell|remind|notify|email)\s+me\b'; then IMPLICIT_CREATE=1; fi
# On-demand recurring (no time period): "give me X whenever/when I ask/say/want"
# Catches "give me a cat fact whenever I ask" — the cat-fact / pg-mood pattern.
if echo "$LMSG" | grep -qE '\b(give|send|share|show|tell|fetch|find|get|grab|pick) me .{3,120}\b(whenever|when|each time|any time|every time)\s+i\s+(ask|say|want|request|need|feel|type|message)'; then IMPLICIT_CREATE=1; fi

# "I'd like / I want / I need / I would love" + periodic time hint anywhere
# in the message — covers verb-less recurring-content asks like
# "I'd like a daily Steve Jobs random fact in the morning".
if echo "$LMSG" | grep -qE "\bi('d| would)?[[:space:]]+(like|want|need|love)\b.{3,120}\b(daily|weekly|hourly|every[[:space:]]+(morning|evening|day|week|hour|night|afternoon|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"; then IMPLICIT_CREATE=1; fi

# "N times a day/week/hour" / "X times daily" — explicit frequency = recurring.
if echo "$LMSG" | grep -qE "\b[0-9]+[[:space:]]*(times?|x)[[:space:]]+(a|per|each)[[:space:]]+(day|week|hour|morning|evening)\b"; then IMPLICIT_CREATE=1; fi

# Periodic time hint paired with a content-type noun anywhere in the
# message (fact, quote, insight, tip, summary, briefing, reminder,
# update, joke, lesson, idea, nugget, wisdom, essay, story, prompt,
# challenge, advice, thought, takeaway). Catches both orderings:
#   "a daily Paul Graham insight"
#   "Paul Graham insight every morning"
NOUN_RE='(fact|quote|insight|insights|tip|tips|summary|briefing|reminder|update|joke|lesson|idea|ideas|nugget|wisdom|essay|story|prompt|challenge|advice|thought|takeaway|takeaways|saying|maxim|aphorism)'
TIME_RE='(daily|weekly|hourly|every[[:space:]]+(morning|evening|day|week|hour|night|afternoon|monday|tuesday|wednesday|thursday|friday|saturday|sunday))'
if echo "$LMSG" | grep -qE "\b${TIME_RE}\b.{0,80}\b${NOUN_RE}\b"; then IMPLICIT_CREATE=1; fi
if echo "$LMSG" | grep -qE "\b${NOUN_RE}\b.{0,40}\b${TIME_RE}\b"; then IMPLICIT_CREATE=1; fi
# Trigger-action pairing: "when I say X, do Y"
if echo "$LMSG" | grep -qE '\bwhen\s+i\s+(say|type|ask|tell|message)\b.{0,80}(reply|respond|send|give|share|tell|do|fetch|return)\b'; then IMPLICIT_CREATE=1; fi
# Capability desire: "I want (you|a way|alfred) to ..."
if echo "$LMSG" | grep -qE '\bi\s+want\s+(you to|a way to|something to|to be able|the bot to|alfred to|you|alfred)\b'; then IMPLICIT_CREATE=1; fi

# --- Implicit refine signals (target a specific existing skill) ---
# "the X skill should also ..." / "modify the X skill ..."
# "this output isn't quite right, fix it so next time ..."
# "you should always do X when I ask for Y" (a refine of how Alfred handles Y)
IMPLICIT_REFINE_TARGET=""
# "the <name> skill should also ..."
T=$(echo "$LMSG" | grep -oE '\bthe[[:space:]]+([a-z0-9_-]+)[[:space:]]+skill\b' | head -1 | awk '{print $2}')
if [ -n "$T" ] && echo "$LMSG" | grep -qE '\b(should|needs to|ought to|must)\b.*\b(also|change|update|fix|tweak|stop|start|include|instead)\b'; then
  IMPLICIT_REFINE_TARGET="$T"
fi
# "modify the <name> skill" / "tweak <name>" / "update the <name> skill"
T2=$(echo "$LMSG" | grep -oE '\b(modify|tweak|update|change|edit|refine|fix)[[:space:]]+(the[[:space:]]+)?([a-z0-9_-]+)[[:space:]]*(skill)?\b' | head -1 | awk '{print $NF}' | sed 's/^the$//')
if [ -z "$IMPLICIT_REFINE_TARGET" ] && [ -n "$T2" ] && [ "$T2" != "skill" ]; then
  IMPLICIT_REFINE_TARGET="$T2"
fi

# Verify the implicit refine target actually exists as a skill — if not,
# treat as create (the user may be referring to a skill they want to
# bring into existence).
if [ -n "$IMPLICIT_REFINE_TARGET" ]; then
  TARGET_EXISTS=0
  for tier in skills openclaw/skills user-skills; do
    if [ -f "$tier/$IMPLICIT_REFINE_TARGET/SKILL.md" ]; then
      TARGET_EXISTS=1
      break
    fi
  done
  if [ "$TARGET_EXISTS" -eq 0 ]; then
    IMPLICIT_REFINE_TARGET=""
    # Re-classify as implicit create (the user is describing something new)
    IMPLICIT_CREATE=1
  fi
fi

if [ -n "$IMPLICIT_REFINE_TARGET" ]; then
  cat <<EOF
INTENT=skill_refine
TARGET=${IMPLICIT_REFINE_TARGET}
CONFIDENCE=75
TRIGGER_SOURCE=implicit_natural_language

REQUIRED_ACTION=The user's natural-language message looks like they want to refine \`${IMPLICIT_REFINE_TARGET}\`. Surface the inference and ask for confirmation in ONE short Telegram reply. Do NOT start the elicit yet — wait for the user's yes/no on the next turn.

EXECUTION_RECIPE:
1. Reply with EXACTLY this text (no other tool calls, no other text):
   "Sounds like you want to tweak \`${IMPLICIT_REFINE_TARGET}\` — want me to walk through a quick refine? (yes / no)"
2. STOP. If the user replies "yes" on the next turn, they should type \`/refine ${IMPLICIT_REFINE_TARGET}\` OR the router will catch their next message as an active-draft continuation if any draft happens to exist.

BANNED_RESPONSES:
- Running elicit.ts directly without the confirmation step.
- Refining a different skill than ${IMPLICIT_REFINE_TARGET}.
- Pre-filling the user's intended diff from their original phrasing.
- Reading source code instead of asking.
EOF
  exit 0
fi

if [ "$IMPLICIT_CREATE" -eq 1 ]; then
  cat <<EOF
INTENT=skill_create
TARGET=
CONFIDENCE=70
TRIGGER_SOURCE=implicit_natural_language

REQUIRED_ACTION=The user's natural-language message looks like a recurring/automatable request. Start the elicit IMMEDIATELY — no opt-in step. The user gets to confirm at the end via the draft-summary "Sound right?" prompt; if they say no there, the draft is discarded and no skill is created. This is simpler and less prone to "yes" being misclassified as something else.

EXECUTION_RECIPE:
1. Run EXACTLY this Bash command (no other tool calls, no preamble text):
   ~/.bun/bin/bun run ~/personal-assistant/meta/skill-author/scripts/elicit.ts --mode=create
2. The script returns a single-line JSON envelope on stdout. Parse it.
3. Take the envelope's \`user_message\` field VERBATIM. Send it as your Telegram reply.
4. STOP. Q2..Q5 come on FUTURE turns once the user answers Q1.

If the envelope says "You have an unfinished draft from … Continue where we left off, or start fresh?" — that is the elicit script's normal resume prompt. RELAY IT VERBATIM and STOP. Do NOT decide "the pipeline is stuck" and write user-skills/ files yourself. The pipeline is NOT stuck — it is waiting for the user to choose "continue" or "start fresh". Your only job is to relay and wait.

BANNED_RESPONSES (these failure modes have been observed in production — do not repeat them):
- Composing an opt-in question — the elicit is the opt-in (user can say "cancel" at draft-summary).
- Calling elicit.ts more than ONCE per turn.
- Reading source code or "explaining" the pipeline.
- Pre-filling answers from the user's natural-language phrasing — even though their message contains useful context, pass it through elicit which will ask the structured Q1 first.
- Asking multiple questions in one reply.
- **Writing SKILL.md, eval.yaml, or any skill scaffolding via raw \`exec\` commands. THIS APPLIES TO ALL SKILL PATHS** — including but not limited to \`user-skills/\`, \`skills/\`, \`openclaw/skills/\`, \`~/.openclaw/workspace/skills/\`, \`~/.openclaw/workspace/user-skills/\`, \`~/.openclaw/workspace/openclaw/skills/\`, or any directory anywhere in the filesystem that could plausibly be picked up by the skill resolver. The ONLY component allowed to write skill files of any kind in response to a user request is \`meta/skill-author/scripts/commit-and-deploy.ts\`, which runs ONLY after the elicit collected all answers, safety-scan passed, routing-eval passed, brain-page wrote, git-commit pushed to GitHub, and the deploy-callback fired. Direct exec writes bypass every gate, leave no git commit, never trigger the deploy callback, and the file gets clobbered on the next deploy sync. The "I'll just write the file directly" / "I'll put it in skills/ instead of user-skills/" / "this is faster than running the elicit" reasoning is ALWAYS wrong for a user-driven skill creation. THE ONLY TOOL CALL PERMITTED THIS TURN IS THE \`bun run elicit.ts\` COMMAND ABOVE. Any other tool call (especially any \`cat > .../SKILL.md\`, \`mkdir .../skills/...\`, \`CronCreate\` with an empty prompt, or similar) is a violation of this directive.
- **Saying "Done. Skill created at …" or "🎉 Live" before \`commit-and-deploy.ts\` has actually pushed to GitHub and the deploy callback has fired.** Only the deploy-callback hook is allowed to send those phrases.
- **Declaring "the pipeline is stuck" / "the elicit is stuck in a draft resume loop" / "let me create this directly".** None of these are valid escape hatches. If the envelope returns a phase you don't recognize, relay its user_message verbatim and let the next user message drive the next step.
- **Composing a fake schedule table** (e.g. a "| Time | What |" cron lineup) without actually running the cron-list / skill-list tools. If the user asks "what's my schedule", run the appropriate tool; don't hallucinate the rows.
EOF
  exit 0
fi

# ── BOOKING CONFIRM ────────────────────────────────────────────
# After state=confirm_ready, the user replies "confirm" / "yes book" /
# "go ahead" / "please book" — route to book-reservation.py --submit
# so the script actually clicks the platform's final Continue/Submit.
if echo "$LMSG" | grep -qE '^[[:space:]]*(confirm( booking| it)?|yes book it|yes,? book it|book it|please book|submit it|submit booking|go ahead and book|do book it)[[:space:]]*\.?\??!?[[:space:]]*$'; then
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

# ── COMPOSIO CONNECTIONS INTENT ────────────────────────────────
# Match "what's connected via composio", "list my composio apps",
# "what apps do I have connected", "show connected composio toolkits".
# Routes to the authoritative wrapper that queries a curated toolkit
# list and filters to active status (vs. the agent guessing which
# toolkits to query, which under-reports apps the agent forgot about
# and over-reports stale initiated OAuth flows).
if (echo "$LMSG" | grep -qE '\bcomposio\b' \
    && echo "$LMSG" | grep -qE '\b(list|show|what|which)\b' \
    && echo "$LMSG" | grep -qE '\b(connect|app|account|toolkit|integration|connection)') \
   || echo "$LMSG" | grep -qE "\bwhat apps (do i|do we|am i|are) connect"; then
  PENDING_FLAG=""
  if echo "$LMSG" | grep -qE '\b(all|pending|initiated|incomplete|stuck|every)\b'; then
    PENDING_FLAG=" --all"
  fi
  cat <<EOF
INTENT=composio_connections

EXECUTION_RECIPE:
1. Run: bash ~/bin/composio-connections.sh${PENDING_FLAG}
2. Relay the script's stdout VERBATIM. The script returns markdown
   grouped by toolkit with email + alias per account.

BANNED_RESPONSES:
- Composing a list from your own memory of what the user has connected.
  Composio's MCP cannot enumerate all toolkits in one call; the wrapper
  queries a curated comprehensive list and is authoritative.
- Reporting "pending/initiated" Slack/GitHub/etc. unless the user
  explicitly asked for all/pending. Default is active-only.
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
