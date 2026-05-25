#!/usr/bin/env bash
# openclaw-telegram-probe-v8.sh — final reconnaissance before the
# "deny message.send" fix lands.
#
# Probe v7 confirmed:
#   - The leak text was agent-composed (not in openclaw source)
#   - humanizer would strip it to empty if invoked
#   - tools.deny exists as an array-of-strings global knob
#   - tools.message.actions.allow exists per-agent
#
# v8 fetches the precise data needed to write the fix:
#   1. agents.list[*].name — identify the "main" agent's index
#   2. The CURRENT value of agents.list[main].tools.message.actions.allow
#      (so we know which actions to keep)
#   3. The global tools.profile's default actions (in case main agent
#      doesn't override)
#   4. The exact registered name of the message tool (for tools.deny
#      as a fallback)
#
# Read-only. Safe. Output → stdout + /tmp/probe-v8.md

set -uo pipefail
OUT=/tmp/probe-v8.md
exec > >(tee "$OUT")
section() { echo; echo "## $1"; echo; }

echo "# openclaw-telegram-probe-v8 — message-tool surface map"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. agents.list[*] — names + indices"
echo "### Full list of configured agents:"
openclaw config get agents.list 2>&1 | python3 -c '
import json, sys
try:
    raw = sys.stdin.read().strip()
    data = json.loads(raw)
    for i, agent in enumerate(data):
        name = agent.get("name", "?")
        model = agent.get("model", "?")
        has_tools = "tools" in agent
        tools_msg = agent.get("tools", {}).get("message")
        print(f"  [{i}] name={name!r} model={model!r} has_tools={has_tools} message_tool_override={bool(tools_msg)}")
except Exception as e:
    print(f"(parse failed: {e})")
    print(raw[:400] if "raw" in dir() else "")
'

section "2. agents.list[main].tools.message — current message-tool config"
echo "### Per-agent message-tool override (if any):"
# Try both 'main' and 'agent.main' since openclaw's CLI may accept either
openclaw config get agents.list 2>&1 | python3 -c '
import json, sys
try:
    raw = sys.stdin.read().strip()
    data = json.loads(raw)
    for agent in data:
        name = agent.get("name", "?")
        tools = agent.get("tools", {})
        msg_tool = tools.get("message")
        print(f"--- agent {name!r} ---")
        if msg_tool:
            print(json.dumps(msg_tool, indent=2))
        else:
            print("(no message-tool override — uses profile defaults)")
        print()
except Exception as e:
    print(f"(parse failed: {e})")
'

section "3. Global tools profile defaults"
echo "### tools (top-level) full config:"
openclaw config get tools 2>&1 | head -40

section "4. Probe the schema for tools.message.actions structure"
SCHEMA=$(mktemp)
openclaw config schema > "$SCHEMA" 2>/dev/null
echo "### Schema for agents.list.*.tools.message.actions:"
jq '.properties.agents.properties.list.items.properties.tools.properties.message.properties.actions' "$SCHEMA" 2>&1 | head -40
echo
echo "### Are there schema enum values for the action names?"
jq -r '
  .properties.agents.properties.list.items.properties.tools.properties.message.properties.actions
  | (if .properties then .properties | keys | .[] else empty end)
' "$SCHEMA" 2>&1 | head -10
echo
echo "### Default value (if any specified):"
jq '.properties.agents.properties.list.items.properties.tools.properties.message.properties.actions.default' "$SCHEMA" 2>&1
rm -f "$SCHEMA"

section "5. What does the messaging profile include by default?"
echo "### Look in openclaw source for messaging profile + default actions:"
OC=/usr/lib/node_modules/openclaw
grep -rln '"messaging"' "$OC"/dist/ 2>/dev/null | head -3 | while read -r f; do
  echo "----- $f -----"
  grep -A 3 '"messaging"' "$f" 2>/dev/null | grep -E 'actions|message|allow|deny' | head -10
done

section "6. The exact strings tools.deny would need to match a generic 'message' tool"
echo "### Tool registry — what's the canonical name?"
OC=/usr/lib/node_modules/openclaw
strings "$OC"/dist/*.js 2>/dev/null | \
  grep -E 'tools?\.message\b|messageTool|"message":\s*\{|"name":\s*"message"' | \
  sort -u | head -10

echo
echo "## Done"
echo "Report: $OUT"
