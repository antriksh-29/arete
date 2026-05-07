#!/bin/bash
# Sends the 22:30 IST daily-task-review prompt to the user.
#
# Newline rule: bash double-quoted "\n" is LITERAL backslash-n, not a
# newline. send-telegram.sh URL-encodes whatever it gets, so a literal
# \n renders as the two characters "\n" in Telegram (the May 7 bug
# screenshot shape). Use ANSI-C $'...' quoting (which interprets \n)
# OR a real linebreak via printf. Never plain double-quoted "\n".
set -euo pipefail

STATE_DIR="$HOME/.brain-bootstrap-raw/state/daily-tasks"
TODAY=$(TZ=Asia/Kolkata date +%Y-%m-%d)
STATE_FILE="$STATE_DIR/$TODAY.json"
[[ -f "$STATE_FILE" ]] || exit 0

TASKS=$(jq -r '.tasks[]' "$STATE_FILE")
[[ -z "$TASKS" ]] && exit 0

# Compose with real newlines via printf -v, not bash "\n" literals.
LINES=()
LINES+=("📋 Daily task review")
LINES+=("")
LINES+=("Reply with status per number (done / tomorrow / later / skip):")
LINES+=("")
INDEX=1
while IFS= read -r TASK; do
  LINES+=("${INDEX}. ${TASK}")
  INDEX=$((INDEX+1))
done <<< "$TASKS"

MSG=$(printf '%s\n' "${LINES[@]}")

/home/botuser/bin/send-telegram.sh "$MSG"
