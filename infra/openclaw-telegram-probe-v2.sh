#!/usr/bin/env bash
# openclaw-telegram-probe-v2.sh — targeted schema dump.
#
# Follow-up to probe v1. v1 found that openclaw already exposes
# message-suppression knobs in its schema:
#   - agents.defaults.silentReply
#   - agents.defaults.silentReplyRewrite
#   - agents.defaults.thinkingDefault
#   - messages.groupChat.visibleReplies      (currently "message_tool")
#   - channels.telegram.streaming.mode       (currently "off" — still leaks)
#
# v2 dumps the schema fragments for these specific knobs so we know
# which value to set them to. No proxy needed if these knobs work.
#
# Read-only. Safe. Output → stdout AND /tmp/openclaw-telegram-probe-v2.md

set -uo pipefail
OUT=/tmp/openclaw-telegram-probe-v2.md
exec > >(tee "$OUT")

section() { echo; echo "## $1"; echo; }

echo "# openclaw-telegram-probe-v2 — schema dump"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

# Dump the full schema once for jq-based extraction.
SCHEMA=$(openclaw config schema 2>/dev/null)
if [ -z "$SCHEMA" ]; then
  echo
  echo "❌ \`openclaw config schema\` returned nothing. Falling back to raw --help."
  openclaw config schema --help 2>&1 | head -30
  exit 0
fi
SCHEMA_FILE=$(mktemp)
echo "$SCHEMA" > "$SCHEMA_FILE"
echo "_Schema size: $(wc -c < "$SCHEMA_FILE") bytes_"

extract() {
  local jq_path="$1"
  local label="$2"
  echo
  echo "### $label"
  echo "**path:** \`$jq_path\`"
  echo
  echo '```json'
  jq "$jq_path" "$SCHEMA_FILE" 2>&1 | head -40
  echo '```'
}

section "1. Suppression / rewrite knobs in agents.defaults"

extract '.properties.agents.properties.defaults.properties.silentReply' \
  "agents.defaults.silentReply"
extract '.properties.agents.properties.defaults.properties.silentReplyRewrite' \
  "agents.defaults.silentReplyRewrite"
extract '.properties.agents.properties.defaults.properties.thinkingDefault' \
  "agents.defaults.thinkingDefault"

section "2. Channel-level streaming knobs"

extract '.properties.channels.properties.telegram.properties.streaming' \
  "channels.telegram.streaming"
extract '.properties.channels.properties.telegram' \
  "channels.telegram (full subtree)"

section "3. Group-chat / visibility knobs"

extract '.properties.messages.properties.groupChat.properties.visibleReplies' \
  "messages.groupChat.visibleReplies"
extract '.properties.messages.properties.groupChat' \
  "messages.groupChat (full subtree)"

section "4. All schema paths mentioning suppression / streaming / reasoning / thinking / intermediate / visible / silent"

echo "Walks the entire schema for any key containing these tokens:"
echo
jq -r '
  paths(scalars) as $p
  | $p[]
  | strings
  | select(test("silent|stream|reason|thinking|intermediate|visible|relay|emit|forward"; "i"))
' "$SCHEMA_FILE" 2>&1 | sort -u | head -40

echo
echo "### Full dot-paths of any key matching the same tokens:"
echo
jq -r '
  [paths | join(".")][]
  | select(test("silent|stream|reason|thinking|intermediate|visible|relay|emit|forward"; "i"))
' "$SCHEMA_FILE" 2>&1 | sort -u | head -40

section "5. Current values of the candidate knobs"

for key in \
  "agents.defaults.silentReply" \
  "agents.defaults.silentReplyRewrite" \
  "agents.defaults.thinkingDefault" \
  "channels.telegram.streaming.mode" \
  "channels.telegram.streaming" \
  "messages.groupChat.visibleReplies" \
; do
  echo
  echo "### $key"
  echo '```'
  openclaw config get "$key" 2>&1 | head -20
  echo '```'
done

section "6. messages subtree schema (where messagePrefix lives — may have neighbor knobs)"

extract '.properties.messages' "messages (full schema)"

echo
echo "## Done"
echo
echo "Report saved to: $OUT ($(wc -l < "$OUT") lines)"
echo
echo "Paste the content back. Goal: identify the right value for"
echo "agents.defaults.silentReply (and/or visibleReplies, streaming.mode)"
echo "that drops pre-tool-call assistant text from reaching Telegram."

rm -f "$SCHEMA_FILE"
