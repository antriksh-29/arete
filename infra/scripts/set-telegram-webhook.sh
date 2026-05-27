#!/usr/bin/env bash
# set-telegram-webhook.sh — point the platform bot's webhook at the router.
#
# Usage:
#   TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... \
#     bash infra/scripts/set-telegram-webhook.sh https://arete.example.com/telegram/webhook
#
# Or to delete (e.g. during dev with another bot fetching updates):
#   TELEGRAM_BOT_TOKEN=... bash infra/scripts/set-telegram-webhook.sh --delete
#
# To inspect what's currently set:
#   TELEGRAM_BOT_TOKEN=... bash infra/scripts/set-telegram-webhook.sh --info

set -euo pipefail

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN required}"

API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

case "${1:-}" in
  --info)
    curl -s "${API}/getWebhookInfo" | jq .
    ;;
  --delete)
    curl -s -X POST "${API}/deleteWebhook?drop_pending_updates=true" | jq .
    ;;
  http*)
    URL="$1"
    EXTRA=""
    if [ -n "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
      EXTRA="&secret_token=${TELEGRAM_WEBHOOK_SECRET}"
    fi
    curl -s -X POST \
      "${API}/setWebhook?url=$(printf '%s' "$URL" | jq -sRr @uri)&allowed_updates=%5B%22message%22%2C%22edited_message%22%2C%22callback_query%22%5D${EXTRA}" \
      | jq .
    ;;
  *)
    echo "usage: $0 <https://your.domain/telegram/webhook>"
    echo "   or: $0 --info"
    echo "   or: $0 --delete"
    exit 2
    ;;
esac
