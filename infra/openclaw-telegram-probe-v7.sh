#!/usr/bin/env bash
# openclaw-telegram-probe-v7.sh — diagnose the tech-narration leak shown
# in the 2026-05-25 video-recommender screenshot.
#
# The bubble: "The command ran successfully. The skill prompt was sent
# to the user (message_id=3504). The user's reply will arrive on a
# future turn with INTENT=skill_periodic_reply."
#
# This reached Telegram even though:
#   - messages.visibleReplies=message_tool (PR #140 — confirmed live)
#   - say.sh has humanizer rules that strip this exact phrasing
#   - messagePrefix bans message(action=send)
#
# So one of:
#   (A) Agent used message(action=send) — bypasses say.sh + humanizer
#   (B) Agent piped through say.sh with wrong --context
#   (C) openclaw itself auto-narrates exec results
#
# This probe distinguishes.
#
# Read-only. Safe. Output → stdout + /tmp/probe-v7.md

set -uo pipefail
OUT=/tmp/probe-v7.md
exec > >(tee "$OUT")
section() { echo; echo "## $1"; echo; }

echo "# openclaw-telegram-probe-v7 — tech-narration leak hunt"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. Does openclaw source contain the leaked strings?"
OC=/usr/lib/node_modules/openclaw
echo "### grep for 'The command ran successfully':"
grep -rln 'The command ran successfully' "$OC" 2>/dev/null | head -5 || echo "(no matches)"
echo
echo "### grep for 'The skill prompt was sent':"
grep -rln 'The skill prompt was sent' "$OC" 2>/dev/null | head -5 || echo "(no matches)"
echo
echo "### grep for 'reply will arrive on a future turn':"
grep -rln 'reply will arrive on a future turn' "$OC" 2>/dev/null | head -5 || echo "(no matches)"
echo
echo "(If none match: text was composed by the agent, not openclaw. Then it's case A or B.)"

section "2. Last 10 outbound Telegram events from openclaw"
echo "### journal entries showing channel=telegram outbound:"
journalctl --user -u openclaw-gateway --since '2 hours ago' --no-pager 2>/dev/null | \
  grep -iE '\[telegram\].*(Outbound|sent|delivered|message_id)' | \
  tail -20
echo
echo "(Look for outbound mentioning 'command ran successfully' or message_id=3504 to identify the path.)"

section "3. message_id=3504 — find the exact send event"
journalctl --user -u openclaw-gateway --since '6 hours ago' --no-pager 2>/dev/null | \
  grep -E 'message_id[=":[:space:]]+3504|3504' | \
  head -10 || echo "(no matches)"

section "4. Did the message go through say.sh? Check ~/.bash_history or audit log"
echo "### grep for say.sh invocations with this text:"
journalctl --user -u openclaw-gateway --since '2 hours ago' --no-pager 2>/dev/null | \
  grep -iE 'say\.sh|humanizer|reply-discipline' | \
  tail -20

section "5. Check the agent's recent message tool calls"
echo "### Look in the openclaw session jsonl for tool_call entries with 'message' or send_text"
SESSIONS_DIR=$HOME/.openclaw/agents/main/sessions
active=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | grep -vE '\.(trajectory|reset|checkpoint)\.' | head -1)
if [ -n "$active" ] && [ -f "$active" ]; then
  echo "Active session: $(basename "$active")"
  echo "### tool_call events mentioning 'message' (last 5):"
  grep -E '"tool_call"|"tool_name"' "$active" 2>/dev/null | \
    grep -iE 'message|send|telegram' | tail -10 || echo "(no matches)"
  echo
  echo "### Outgoing assistant messages — first 200 chars of each (last 5):"
  python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    rows = [json.loads(l) for l in f if l.strip()]
# Find assistant messages
asst = [r for r in rows if r.get("role") == "assistant" or r.get("type") == "assistant"]
for r in asst[-5:]:
    content = r.get("content") or r.get("text") or r.get("body") or ""
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    print(f"  - [{r.get(\"timestamp\", \"?\")}] {str(content)[:200]}")
' "$active" 2>&1 | head -30
fi

section "6. Schema dump — is there a way to disable native message tool?"
SCHEMA=$(mktemp)
openclaw config schema > "$SCHEMA" 2>/dev/null
echo "### Schema paths matching 'message' under tools / agents:"
jq -r '
  [paths | join(".")][]
  | select(test("tools.*message|agents.*messageTool|tools.deny|messageTool|sendTool"; "i"))
' "$SCHEMA" 2>&1 | sort -u | head -20
echo
echo "### Specifically: does tools.deny take a string for blocking specific tools?"
jq '.properties.tools.properties.deny' "$SCHEMA" 2>&1 | head -20
echo
echo "### Look for any tool with 'message' in its name we could add to deny list:"
openclaw config get tools 2>&1 | head -30
rm -f "$SCHEMA"

section "7. Verify say.sh + humanizer didn't fire on this specific text"
echo "### Live regex test of humanizer against the leak phrase:"
echo 'The command ran successfully. The skill prompt was sent to the user (message_id=3504). The user'\''s reply will arrive on a future turn with INTENT=skill_periodic_reply.' | \
  python3 "$HOME/bin/humanizer.py" --context chat 2>&1 | \
  python3 -c 'import json,sys; r=json.load(sys.stdin); print("INPUT len:", len(r["input"])); print("OUTPUT len:", len(r["output"])); print("OUTPUT text:", repr(r["output"][:200]))'

echo
echo "## Done"
echo "Report: $OUT"
