#!/bin/bash
# Schedule a one-shot Telegram reminder via systemd-run.
#
# Usage:
#   schedule-reminder.sh "30m"  "ping me about thing X"     # relative
#   schedule-reminder.sh "in 1h" "lunch reminder"            # relative natural
#   schedule-reminder.sh "@2026-04-30 21:30:00" "kaushik nudge"  # absolute IST
#   schedule-reminder.sh "2026-04-30T21:30:00+05:30" "..."   # absolute ISO
#
# `systemd-run --user --on-active=...` for relative; `--on-calendar=...` for absolute.
# Each scheduled reminder gets a unique unit name so we can list / cancel later.
#
# Reminder context is also appended to ~/.brain/_state/reminders.jsonl for auditing
# (path is outside ~/.brain so gbrain doesn't try to import it as a page).

set -e

WHEN="$1"
shift
MSG="$*"

if [ -z "$WHEN" ] || [ -z "$MSG" ]; then
  echo "Usage: schedule-reminder.sh <when> <message>" >&2
  exit 1
fi

UNIT_NAME="reminder-$(date +%s)-$$"
SCRIPT="/home/botuser/bin/send-telegram.sh"

if [[ "$WHEN" == @* ]]; then
  # Calendar form: @YYYY-MM-DD HH:MM[:SS]   (interpreted as Asia/Kolkata)
  CAL="${WHEN#@}"
  systemd-run --user --unit "$UNIT_NAME" --on-calendar="$CAL Asia/Kolkata" \
    --description="Reminder: ${MSG:0:80}" \
    "$SCRIPT" "⏰ Reminder: $MSG" >&2
elif [[ "$WHEN" == [0-9]* && "$WHEN" =~ ^[0-9]+[smhd]$ ]]; then
  # Relative simple: 30m, 1h, 90s, 2d
  systemd-run --user --unit "$UNIT_NAME" --on-active="$WHEN" \
    --description="Reminder: ${MSG:0:80}" \
    "$SCRIPT" "⏰ Reminder: $MSG" >&2
elif [[ "$WHEN" == in\ * ]]; then
  # "in 1h" / "in 30m" / "in 5min"
  RAW="${WHEN#in }"
  RAW="${RAW// /}"
  RAW="${RAW//min/m}"
  RAW="${RAW//mins/m}"
  RAW="${RAW//hours/h}"
  RAW="${RAW//hour/h}"
  systemd-run --user --unit "$UNIT_NAME" --on-active="$RAW" \
    --description="Reminder: ${MSG:0:80}" \
    "$SCRIPT" "⏰ Reminder: $MSG" >&2
else
  # ISO 8601 absolute. systemd-run --on-calendar accepts these directly.
  systemd-run --user --unit "$UNIT_NAME" --on-calendar="$WHEN" \
    --description="Reminder: ${MSG:0:80}" \
    "$SCRIPT" "⏰ Reminder: $MSG" >&2
fi

# Audit log
mkdir -p "$HOME/.brain-bootstrap-raw/state"
JSON="{\"unit\":\"$UNIT_NAME\",\"when\":\"$WHEN\",\"msg\":$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"scheduled_at\":\"$(date -Iseconds)\"}"
echo "$JSON" >> "$HOME/.brain-bootstrap-raw/state/reminders.jsonl"

echo "$UNIT_NAME"
