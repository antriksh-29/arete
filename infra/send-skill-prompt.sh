#!/usr/bin/env bash
# send-skill-prompt.sh — deterministic send of a periodic-skill question
# message to the user, with the provenance tag + state-file marker that
# the reply router relies on.
#
# Pattern: skills that ask the user a question on a cron (e.g.
# "what kind of video do you want today?") used to be wired to
# OpenClaw's `payload.kind=agentTurn`. The agent composed the message
# but nothing tied it to a slug, so when the user tap-replied hours
# later, the bot had lost the link to the skill and gave a generic
# answer. This script replaces the agentTurn for the *prompt* leg:
#
#   1. Appends a visible "↩ reply to /<slug>" footer (HTML italic).
#   2. Writes ~/.brain-bootstrap-raw/state/last-periodic-skill.json
#      so intent-router.sh can route the user's next would-be-chat
#      message to the originating skill within a 15-min window.
#   3. Sends via send-telegram.sh --html.
#
# Usage:
#   send-skill-prompt.sh <slug> "<question text>"
#
# Idempotent. Exits non-zero on send failure.

set -euo pipefail

SLUG="${1:-}"
BODY="${2:-}"
if [ -z "$SLUG" ] || [ -z "$BODY" ]; then
  echo "usage: send-skill-prompt.sh <slug> '<question text>'" >&2
  exit 2
fi

STATE_DIR="$HOME/.brain-bootstrap-raw/state"
STATE_FILE="$STATE_DIR/last-periodic-skill.json"
SEND_TG="$HOME/bin/send-telegram.sh"

mkdir -p "$STATE_DIR"

# Escape user-supplied slug for HTML (the body is assumed to be plain
# text from the caller — escape it before composing the HTML message).
esc() {
  python3 -c 'import html,sys;print(html.escape(sys.argv[1]),end="")' "$1"
}
SLUG_ESC=$(esc "$SLUG")
BODY_ESC=$(esc "$BODY")
FOOTER="<i>↩ reply to keep talking to /${SLUG_ESC}</i>"

MSG="${BODY_ESC}

${FOOTER}"

# Write state BEFORE sending so a fast user reply still finds the file.
python3 - "$SLUG" "$BODY" "$STATE_FILE" <<'PY'
import json, os, sys, time
slug, body, path = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({
    "slug": slug,
    "sent_at": int(time.time()),
    "message_preview": body[:240],
}, open(path, "w"), indent=2)
PY

"$SEND_TG" --html "$MSG"
echo "[$(date -Iseconds)] sent skill-prompt for /$SLUG"
