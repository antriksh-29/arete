#!/bin/bash
# Send a Telegram message to the user.
# Used by the reminder system and any other one-shot notification path.
#
# Usage:
#   send-telegram.sh "message text"
#   send-telegram.sh -                 # reads from stdin
#   send-telegram.sh --html "<b>bold</b> body"     # HTML parse mode
#   send-telegram.sh --html -          # HTML from stdin
#
# HTML mode supports Telegram's restricted HTML subset: <b>, <i>, <u>,
# <s>, <code>, <pre>, <a href="...">, <blockquote>. Caller MUST escape
# any user-supplied &, <, > characters before passing them in.

set -e
TOKEN="$(/usr/local/bin/bot-cred get TELEGRAM_BOT_TOKEN)"
USER_ID="$(/usr/local/bin/bot-cred get TELEGRAM_USER_ID)"

if [ -z "$TOKEN" ] || [ -z "$USER_ID" ]; then
  echo "ABORT: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID missing from vault" >&2
  exit 1
fi

PARSE_MODE=""
if [ "${1:-}" = "--html" ]; then
  PARSE_MODE="HTML"
  shift
fi

if [ "${1:-}" = "-" ]; then
  MSG=$(cat)
else
  MSG="$*"
fi

if [ -z "$MSG" ]; then
  echo "ABORT: empty message" >&2
  exit 1
fi

# Default to plain text (no parse_mode). Telegram's Markdown parser is
# strict and rejects messages with unbalanced *, _, [, ` (HTTP 400 /
# silent delivery failure). HTML is opt-in via --html for callers that
# control their content and want bold / italic / blockquote.
ARGS=("-sf" "-X" "POST" "https://api.telegram.org/bot${TOKEN}/sendMessage"
      "--data-urlencode" "chat_id=${USER_ID}"
      "--data-urlencode" "text=${MSG}")
if [ -n "$PARSE_MODE" ]; then
  ARGS+=("--data-urlencode" "parse_mode=${PARSE_MODE}")
fi

curl "${ARGS[@]}" >/dev/null

echo "[$(date -Iseconds)] sent telegram${PARSE_MODE:+ (parse_mode=$PARSE_MODE)}: ${MSG:0:80}"
