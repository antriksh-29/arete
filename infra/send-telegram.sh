#!/bin/bash
# Send a Telegram message to the user.
# Used by the reminder system and any other one-shot notification path.
#
# Usage: send-telegram.sh "message text"
#        send-telegram.sh -                 # reads message from stdin

set -e
TOKEN="$(/usr/local/bin/bot-cred get TELEGRAM_BOT_TOKEN)"
USER_ID="$(/usr/local/bin/bot-cred get TELEGRAM_USER_ID)"

if [ -z "$TOKEN" ] || [ -z "$USER_ID" ]; then
  echo "ABORT: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID missing from vault" >&2
  exit 1
fi

if [ "$1" = "-" ]; then
  MSG=$(cat)
else
  MSG="$*"
fi

if [ -z "$MSG" ]; then
  echo "ABORT: empty message" >&2
  exit 1
fi

curl -sf -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${USER_ID}" \
  --data-urlencode "text=${MSG}" \
  --data-urlencode "parse_mode=Markdown" >/dev/null

echo "[$(date -Iseconds)] sent telegram: ${MSG:0:80}"
