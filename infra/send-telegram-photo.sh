#!/bin/bash
# Send a photo (PNG/JPEG) to the user via the Telegram bot.
# Uses the Telegram Bot API's `sendPhoto` endpoint over multipart form-data.
# Falls back to `sendDocument` automatically for files >10MB (the
# sendPhoto limit) so big full-page screenshots still arrive.
#
# Usage:
#   send-telegram-photo.sh <path> [caption]
#   send-telegram-photo.sh /tmp/shot.png "form filled — review before submit"
#
# Exit codes:
#   0 — sent
#   1 — file missing / vault unavailable / curl failed

set -e

PATH_TO_PHOTO="${1:-}"
CAPTION="${2:-}"

if [ -z "$PATH_TO_PHOTO" ]; then
  echo "Usage: send-telegram-photo.sh <path> [caption]" >&2
  exit 1
fi
if [ ! -f "$PATH_TO_PHOTO" ]; then
  echo "ABORT: file not found: $PATH_TO_PHOTO" >&2
  exit 1
fi

TOKEN="$(/usr/local/bin/arete-cred get TELEGRAM_BOT_TOKEN)"
USER_ID="$(/usr/local/bin/arete-cred get TELEGRAM_USER_ID)"
if [ -z "$TOKEN" ] || [ -z "$USER_ID" ]; then
  echo "ABORT: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID missing from vault" >&2
  exit 1
fi

# Pick endpoint based on size. sendPhoto caps at 10MB; sendDocument
# goes to 50MB. Use stat -c on Linux.
SIZE=$(stat -c %s "$PATH_TO_PHOTO" 2>/dev/null || stat -f %z "$PATH_TO_PHOTO")
LIMIT=$((10 * 1024 * 1024))

if [ "$SIZE" -gt "$LIMIT" ]; then
  ENDPOINT="sendDocument"
  FIELD="document"
else
  ENDPOINT="sendPhoto"
  FIELD="photo"
fi

# Build curl args. -F handles multipart, -sS keeps it quiet but shows
# errors, -w prints HTTP status as the final line.
CURL_ARGS=(
  -sS -o /tmp/.tg-photo-resp.json -w "%{http_code}"
  -X POST
  "https://api.telegram.org/bot${TOKEN}/${ENDPOINT}"
  -F "chat_id=${USER_ID}"
  -F "${FIELD}=@${PATH_TO_PHOTO}"
)
if [ -n "$CAPTION" ]; then
  CURL_ARGS+=(-F "caption=${CAPTION}")
fi

HTTP_CODE=$(curl "${CURL_ARGS[@]}")

if [ "$HTTP_CODE" != "200" ]; then
  echo "ABORT: Telegram ${ENDPOINT} returned HTTP $HTTP_CODE" >&2
  head -c 500 /tmp/.tg-photo-resp.json >&2
  echo >&2
  exit 1
fi

echo "[$(date -Iseconds)] sent telegram ${ENDPOINT}: ${PATH_TO_PHOTO} (${SIZE} bytes)"
