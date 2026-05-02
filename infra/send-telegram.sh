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

# NOTE: parse_mode intentionally omitted. Telegram's Markdown parser rejects
# messages with unbalanced *, _, [, or ` characters with HTTP 400 (curl exit
# 22), causing silent delivery failure. Sending as plain text is more robust;
# if formatting is needed in the future, switch to MarkdownV2 + escape
# user-supplied content through a sanitizer.
curl -sf -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${USER_ID}" \
  --data-urlencode "text=${MSG}" >/dev/null

echo "[$(date -Iseconds)] sent telegram: ${MSG:0:80}"
