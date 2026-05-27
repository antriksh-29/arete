#!/usr/bin/env bash
# reply-send-wrapper.sh — gates an outbound Telegram reply through the
# reply-discipline filter before sending. Stand-alone tool; the caller
# decides what to do on rejection (retry with a different prompt, fall
# back to a safe template, etc.).
#
# Reads candidate text from stdin. Sends only if reply-discipline says
# ok=true. Exit codes:
#   0 — sent successfully
#   1 — reply-discipline rejected (no send); rejection envelope on stdout
#   2 — Telegram API failed (no send); error envelope on stdout
#   3 — usage / config error
#
# Required env (on VPS):
#   BOT_TOKEN   Telegram bot token (or pulled via arete-cred get BOT_TOKEN)
#   CHAT_ID     Telegram chat id (or passed as arg 1)
#
# Usage:
#   echo "<reply text>" | reply-send-wrapper.sh <chat_id>
#   echo "<reply text>" | CHAT_ID=12345 reply-send-wrapper.sh
#   reply-send-wrapper.sh --smoke

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"reply-send-wrapper","user_message":"reply-send-wrapper smoke ok"}'
  exit 0
fi

CHAT_ID="${1:-${CHAT_ID:-}}"
if [ -z "$CHAT_ID" ]; then
  echo '{"ok":false,"reason":"no chat_id (pass as arg1 or set CHAT_ID env)","user_message":"usage error"}' >&2
  exit 3
fi

# ── pull bot token ──────────────────────────────────────────────
BOT_TOKEN="${BOT_TOKEN:-}"
if [ -z "$BOT_TOKEN" ] && command -v arete-cred >/dev/null 2>&1; then
  BOT_TOKEN="$(arete-cred get BOT_TOKEN 2>/dev/null || true)"
fi
if [ -z "$BOT_TOKEN" ]; then
  echo '{"ok":false,"reason":"BOT_TOKEN unset and arete-cred unavailable","user_message":"config error"}' >&2
  exit 3
fi

# ── read candidate from stdin ───────────────────────────────────
CANDIDATE="$(cat)"

# Locate reply-discipline. Prefer the deployed VPS path (~/bin/) since
# this wrapper runs on the VPS; fall back to the repo's infra/ for
# local testing.
if [ -x "$HOME/bin/reply-discipline.py" ]; then
  RD="$HOME/bin/reply-discipline.py"
elif [ -x "$SCRIPT_DIR/reply-discipline.py" ]; then
  RD="$SCRIPT_DIR/reply-discipline.py"
else
  echo '{"ok":false,"reason":"reply-discipline.py not found","user_message":"config error"}' >&2
  exit 3
fi

# ── gate the candidate ──────────────────────────────────────────
VERDICT="$(printf '%s' "$CANDIDATE" | python3 "$RD")"
OK="$(printf '%s' "$VERDICT" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["ok"])')"

if [ "$OK" != "True" ]; then
  # Rejected — surface the full reply-discipline envelope. Caller
  # decides whether to retry with the suggestion.
  echo "$VERDICT"
  exit 1
fi

# ── send via Telegram API ───────────────────────────────────────
RESP="$(curl -sS -X POST \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${CANDIDATE}" \
  -d "parse_mode=Markdown" \
  --max-time 15)" || {
  echo "{\"ok\":false,\"reason\":\"telegram api curl failed\",\"user_message\":\"send failed\"}" >&2
  exit 2
}

OK_TG="$(printf '%s' "$RESP" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d.get("ok"))' 2>/dev/null || echo False)"
if [ "$OK_TG" != "True" ]; then
  echo "{\"ok\":false,\"reason\":\"telegram api returned non-ok\",\"telegram_response\":$RESP,\"user_message\":\"send failed\"}" >&2
  exit 2
fi

# Success
MSG_ID="$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["result"]["message_id"])' 2>/dev/null || echo null)"
echo "{\"ok\":true,\"sent\":true,\"chat_id\":${CHAT_ID},\"message_id\":${MSG_ID},\"user_message\":\"sent\"}"
exit 0
