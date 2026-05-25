#!/usr/bin/env bash
# openclaw-telegram-probe-v4.sh — find the source of the
# "🔄 I rotated to a fresh session..." message and the knobs to
# suppress it / reduce its frequency.
#
# Background: this message leaks to Telegram even though SOUL.md
# explicitly bans it and humanizer.py has a rule to strip it.
# That means it's emitted by a path that doesn't go through say.sh
# or the agent's normal reply pipeline — probably an openclaw
# framework-level notification fired at the channel layer.
#
# Read-only. Safe.
# Output → stdout + /tmp/openclaw-telegram-probe-v4.md

set -uo pipefail
OUT=/tmp/openclaw-telegram-probe-v4.md
exec > >(tee "$OUT")

section() { echo; echo "## $1"; echo; }

echo "# openclaw-telegram-probe-v4 — session-rotation source hunt"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. Grep the openclaw source for the rotation message"
echo "### where does 'rotated to a fresh session' come from?"
OPENCLAW_DIR=/usr/lib/node_modules/openclaw
if [ -d "$OPENCLAW_DIR" ]; then
  echo "OpenClaw source dir: $OPENCLAW_DIR"
  echo
  # The bundled dist is one big JS file; grep for the literal phrase.
  echo "### Files containing 'rotated to a fresh session':"
  grep -rln 'rotated to a fresh session\|rotated to a new session\|conversation had grown' \
    "$OPENCLAW_DIR" 2>/dev/null | head -10
  echo
  echo "### context around the phrase (first 5 occurrences, ~5 lines each):"
  grep -rln 'rotated to a fresh session' "$OPENCLAW_DIR" 2>/dev/null | while read -r f; do
    echo "----- $f -----"
    grep -n 'rotated to a fresh session\|conversation had grown\|context limit\|brain pages' "$f" 2>/dev/null | head -10
    echo
  done | head -60
  echo
  echo "### Other plausible phrasings nearby:"
  grep -hoE '"[^"]{20,160}"' "$OPENCLAW_DIR"/dist/*.js 2>/dev/null | \
    grep -iE 'rotated|fresh session|context limit|brain page' | sort -u | head -20
else
  echo "(openclaw dir not at $OPENCLAW_DIR — try $(which openclaw))"
fi

section "2. Schema dump for session.* and any 'rotat' / 'restart' / 'notification' paths"
SCHEMA_FILE=$(mktemp)
openclaw config schema > "$SCHEMA_FILE" 2>/dev/null

echo "### .properties.session (top-level):"
jq '.properties.session // "(missing)"' "$SCHEMA_FILE" 2>&1 | head -60
echo
echo "### .properties.agents.properties.defaults (full key list — there may be more than v2 enumerated):"
jq '.properties.agents.properties.defaults.properties | keys' "$SCHEMA_FILE" 2>&1
echo
echo "### Every schema path containing 'rotat' or 'restart' or 'notification' or 'session' or 'transcript':"
jq -r '
  [paths | join(".")][]
  | select(test("rotat|restart|notification|session|transcript|main-session|messageLog"; "i"))
' "$SCHEMA_FILE" 2>&1 | sort -u | head -50
echo
echo "### Same scan for VALUES (enum strings / consts):"
jq -r '
  paths(scalars) as $p
  | getpath($p)
  | strings
  | select(test("rotat|fresh session|context limit|brain pages|conversation.*grown|main-session"; "i"))
' "$SCHEMA_FILE" 2>&1 | sort -u | head -20

rm -f "$SCHEMA_FILE"

section "3. openclaw hooks subcommand surface"
openclaw hooks --help 2>&1 | head -50
echo
echo "### Current hooks (if any):"
openclaw hooks list 2>&1 | head -20

section "4. Current values of suspect session-rotation knobs"
for key in \
  "session" \
  "agents.defaults.bootstrapMaxChars" \
  "agents.defaults.bootstrapTotalMaxChars" \
  "agents.defaults.timeoutSeconds" \
  "agents.defaults.silentReply" \
  "agents.defaults.silentReplyRewrite" \
  "messages.systemNotifications" \
  "messages.rotationNotifications" \
  "channels.telegram.silentErrorReplies" \
; do
  echo "### $key"
  v=$(openclaw config get "$key" 2>&1 | head -10)
  echo "$v" | head -8
  echo
done

section "5. Recent journal events for session-rotation / main-session-restart"
journalctl --user -u openclaw-gateway -n 2000 --no-pager 2>/dev/null | \
  grep -iE 'rotat|main-session|main session|transcript tail|session-restart|context limit|fresh session|recovery' | \
  tail -30

section "6. Outbound messages that contain the rotation phrase (proof of which path emits it)"
journalctl --user -u openclaw-gateway -n 2000 --no-pager 2>/dev/null | \
  grep -B1 -A1 -iE 'rotated to a fresh session|conversation had grown' | \
  tail -40

section "7. Is there a per-channel suppression in channels.telegram?"
openclaw config get channels.telegram 2>&1 | head -40

section "8. silentErrorReplies — schema fragment"
SCHEMA_FILE2=$(mktemp)
openclaw config schema > "$SCHEMA_FILE2" 2>/dev/null
jq '.. | objects | select(.silentErrorReplies?) | .silentErrorReplies' "$SCHEMA_FILE2" 2>&1 | head -20
rm -f "$SCHEMA_FILE2"

echo
echo "## Done"
echo "Report saved to: $OUT ($(wc -l < "$OUT") lines)"
