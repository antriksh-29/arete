#!/bin/bash
# Wrapper for clawvisor-mcp as OpenClaw MCP child.
# OpenClaw strips env when spawning MCP children, so we inject ClawVisor
# credentials from the bot-cred vault before exec.
# Same pattern as gbrain-mcp-wrapper.sh.

export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export CLAWVISOR_URL="$(/usr/local/bin/bot-cred get CLAWVISOR_URL 2>/dev/null)"
export CLAWVISOR_AGENT_TOKEN="$(/usr/local/bin/bot-cred get CLAWVISOR_AGENT_TOKEN 2>/dev/null)"
export CLAWVISOR_STANDING_TASK_ID="$(/usr/local/bin/bot-cred get CLAWVISOR_STANDING_TASK_ID 2>/dev/null)"

exec /usr/bin/node /home/botuser/clawvisor-mcp/server.mjs "$@"
