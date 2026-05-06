#!/bin/bash
# Fire the signal-detector sub-agent in the background and exit immediately.
# Main agent calls this as its first tool call on any non-operational message.
# The sub-agent reads the user message, captures originals/entities/ideas
# to brain via gbrain MCP, exits. Output streamed to a log for debugging.
#
# Usage: spawn-signal-detector.sh "<the user's message verbatim>"

set -e

USER_MSG="${1:-}"
if [ -z "$USER_MSG" ]; then
  echo "spawn-signal-detector: no message" >&2
  exit 0
fi

# Skip purely operational messages — cheap pre-filter so we don't burn
# tokens on "yes" / "ok" / single-emoji replies. The sub-agent itself
# also filters but this is faster and saves cost.
TRIM=$(echo -n "$USER_MSG" | tr -d '[:space:]')
if [ ${#TRIM} -lt 8 ]; then
  exit 0
fi
case "$(echo "$USER_MSG" | tr '[:upper:]' '[:lower:]' | tr -d '[:punct:]' | xargs)" in
  yes|yeah|yep|ok|okay|sure|thanks|thx|nope|no|cool|noted|got\ it|done|approve|approved|reject|rejected|send\ it|let\ us\ go|lets\ go)
    exit 0
    ;;
esac

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs/signal-detector}"
mkdir -p "$LOG_DIR"
TS=$(date -Iseconds)
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

echo "[$TS] spawning capture for: ${USER_MSG:0:120}" >> "$LOG"

# Background detached run — main agent's exec returns immediately.
# nohup + & + redirect = fire-and-forget.
nohup openclaw agent \
  --agent signal-detector \
  --message "$USER_MSG" \
  --json \
  >> "$LOG" 2>&1 &

echo "ok: signal-detector spawned (pid $!, log $LOG)"
