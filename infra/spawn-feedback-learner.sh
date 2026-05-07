#!/bin/bash
# Fire the feedback-learner sub-agent in the background and exit immediately.
# Main agent calls this on messages that look like behavior corrections.
# The sub-agent extracts the rule, drafts a proposed AGENTS.md edit, writes
# to a pending queue. User approves/discards via main-agent routing.
#
# Usage: spawn-feedback-learner.sh "<the user's message verbatim>"

set -e

USER_MSG="${1:-}"
if [ -z "$USER_MSG" ]; then
  echo "spawn-feedback-learner: no message" >&2
  exit 0
fi

# Pre-filter: skip messages with no feedback markers. Cheaper than burning
# tokens on an LLM that will say "no capture". Sub-agent does the real
# detection — this is just an obvious-no filter.
LOWER=$(echo "$USER_MSG" | tr '[:upper:]' '[:lower:]')
if ! echo "$LOWER" | grep -qE 'next time|going forward|from now on|stop doing|always|never|don.?t ever|you should always|in future|make sure you|remember to|don.?t do that|never do that|stop sending|stop using|use .* instead|prefer .* over'; then
  exit 0
fi

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs/feedback-learner}"
QUEUE_DIR="$HOME/.brain-bootstrap-raw/feedback-queue"
mkdir -p "$LOG_DIR" "$QUEUE_DIR" "$QUEUE_DIR/applied" "$QUEUE_DIR/discarded"
TS=$(date -Iseconds)
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

echo "[$TS] spawning feedback-learner for: ${USER_MSG:0:120}" >> "$LOG"

nohup openclaw agent \
  --agent feedback-learner \
  --message "$USER_MSG" \
  --json \
  >> "$LOG" 2>&1 &

echo "ok: feedback-learner spawned (pid $!, log $LOG)"
