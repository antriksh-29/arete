#!/bin/bash
# reset-tg-session.sh — clear the polluted Telegram-channel session so
# the next inbound message starts fresh.
#
# Background: each Telegram message from a user→bot direct chat is
# pinned to one openclaw session keyed `agent:main:telegram:direct:<chat_id>`.
# That session accumulates history across every turn, and after 20+
# turns it bloats to >400 KB. Bigger history → slower turns → cascading
# timeouts in rapid test runs. This script:
#   1. Backs up sessions.json
#   2. Looks up the session-id for the Telegram chat
#   3. Removes the mapping from sessions.json (in-place edit)
#   4. Moves the .jsonl + .trajectory.jsonl files to .attic-* (kept
#      for forensics; gateway will not see them)
#   5. Restarts openclaw-gateway so the in-memory mapping is dropped
#   6. Verifies the key is gone
#
# Usage:
#   bash infra/reset-tg-session.sh                   # interactive: shows
#                                                    # current state, asks
#                                                    # before nuking
#   bash infra/reset-tg-session.sh --yes             # non-interactive
#   bash infra/reset-tg-session.sh --chat-id 12345   # different user

set -uo pipefail

CHAT_ID=""
CONFIRM=1
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) CONFIRM=0 ;;
    --chat-id) shift; CHAT_ID="${1:-}" ;;
    --*) ;;  # ignore unknown flags
    *) CHAT_ID="$1" ;;
  esac
  shift
done
CHAT_ID="${CHAT_ID:-775188536}"

KEY="agent:main:telegram:direct:${CHAT_ID}"
SESSIONS_JSON="$HOME/.openclaw/agents/main/sessions/sessions.json"
SESSIONS_DIR="$HOME/.openclaw/agents/main/sessions"

if [ ! -f "$SESSIONS_JSON" ]; then
  echo "ERROR: sessions.json not found at $SESSIONS_JSON" >&2
  exit 1
fi

# Look up the session-id for this chat
SESSION_ID=$(python3 -c "
import json, sys
d = json.load(open('$SESSIONS_JSON'))
e = d.get('$KEY')
print(e.get('sessionId','') if e else '', end='')
" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
  echo "no telegram session for chat $CHAT_ID — nothing to reset"
  exit 0
fi

SESSION_FILE="$SESSIONS_DIR/$SESSION_ID.jsonl"
SIZE=$(stat -c%s "$SESSION_FILE" 2>/dev/null || echo 0)
SIZE_KB=$((SIZE / 1024))
TURNS=$(wc -l < "$SESSION_FILE" 2>/dev/null || echo 0)

echo "Telegram session for chat $CHAT_ID:"
echo "  session_id:  $SESSION_ID"
echo "  jsonl file:  $SESSION_FILE"
echo "  size:        ${SIZE_KB} KB ($TURNS lines)"
echo

if [ "$CONFIRM" -eq 1 ]; then
  read -p "Reset this session? Files will move to .attic-* (recoverable). [y/N] " ans
  if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
    echo "aborted"; exit 0
  fi
fi

STAMP=$(date +%Y%m%d-%H%M%S)
echo "[1/4] backup sessions.json → sessions.json.bak.$STAMP"
cp "$SESSIONS_JSON" "$SESSIONS_JSON.bak.$STAMP"

echo "[2/4] remove $KEY from sessions.json"
python3 -c "
import json
p = '$SESSIONS_JSON'
d = json.load(open(p))
d.pop('$KEY', None)
json.dump(d, open(p,'w'), indent=2)
"

echo "[3/4] move session files to .attic-* (kept for forensics)"
for ext in jsonl jsonl.codex-app-server.json trajectory.jsonl trajectory-path.json; do
  src="$SESSIONS_DIR/$SESSION_ID.$ext"
  if [ -e "$src" ]; then
    mv "$src" "$SESSIONS_DIR/.attic-$STAMP-$SESSION_ID.$ext"
  fi
done

echo "[4/4] restart openclaw-gateway so in-memory mapping is dropped"
systemctl --user restart openclaw-gateway.service
sleep 5
if systemctl --user is-active openclaw-gateway.service >/dev/null; then
  echo "  gateway: active"
else
  echo "  WARN: gateway not active after restart"
fi

echo
echo "✓ session reset. Next message from chat $CHAT_ID creates a fresh session."
