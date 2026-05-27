#!/bin/bash
# Sends the 22:00 IST daily-task-review prompt to the user.
#
# Reads today's task list via the canonical script. Works against both
# v1 flat files (legacy) and v2 per-task lineage (post-migration) — the
# `daily-tasks.sh show <date>` command transparently falls back.
#
# Newline rule: bash double-quoted "\n" is LITERAL backslash-n, not a
# newline. send-telegram.sh URL-encodes whatever it gets, so a literal
# \n renders as the two characters "\n" in Telegram (the May 7 bug
# screenshot shape). Use ANSI-C $'...' quoting (which interprets \n)
# OR a real linebreak via printf. Never plain double-quoted "\n".
set -euo pipefail

TODAY=$(TZ=Asia/Kolkata date +%Y-%m-%d)

# Pull today's task titles via the canonical script. We use Python's
# `status` subcommand (JSON output) for stable parsing; falls back to
# v1 flat file for the migration window.
V2_DAILY="$HOME/.brain-bootstrap-raw/state/daily-tasks/v2/daily/$TODAY.json"
V1_FILE="$HOME/.brain-bootstrap-raw/state/daily-tasks/$TODAY.json"

TASKS=""
if [ -f "$V2_DAILY" ]; then
  # v2 layout — read task titles via the Python helper. JSON 'tasks'
  # array contains task dicts with .title.
  TASKS=$(python3 "$HOME/bin/daily-tasks.py" status \
    | jq -r '.tasks[].title' 2>/dev/null || true)
elif [ -f "$V1_FILE" ]; then
  # v1 layout — read directly from the flat file.
  TASKS=$(jq -r '.tasks[]' "$V1_FILE" 2>/dev/null || true)
fi

# Filter: skip tasks already in a terminal state (yes/delete) so the
# review prompt only asks about what's still open. v1 doesn't track
# per-task status emoji directly, so we rely on the script's `show`
# omission of terminal tasks in v2. For v1, all tasks are still listed.
[[ -z "$TASKS" ]] && exit 0

# Compose with real newlines via printf, not bash "\n" literals.
# The "for <YYYY-MM-DD>" suffix is load-bearing: the bot extracts it
# from the prompt message and passes --date to `daily-tasks.sh review`
# so a late reply (e.g. at 02:00 the next day) still applies to the
# correct date. See skills/daily-task-manager/SKILL.md.
LINES=()
LINES+=("📋 Daily task review for $TODAY")
LINES+=("")
LINES+=("Reply with status per number (done / tomorrow / later / skip):")
LINES+=("")
INDEX=1
while IFS= read -r TASK; do
  LINES+=("${INDEX}. ${TASK}")
  INDEX=$((INDEX+1))
done <<< "$TASKS"

MSG=$(printf '%s\n' "${LINES[@]}")

${STATE_HOME}/bin/send-telegram.sh "$MSG"
