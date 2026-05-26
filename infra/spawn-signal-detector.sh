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

# Skip the heavy signal-detector spawn on cheap operational messages
# ("yes", "ok", short single-word replies). The intent-router below
# is ALWAYS run — without it, "yes" / "no" / "cancel" replies that
# confirm an active skill-author draft lose their routing directive
# and the main agent improvises (observed 2026-05-21: ship-path test
# failed because spawn-signal-detector exited early on "yes" → router
# output dropped → agent re-ran refine-diff and overwrote the draft).
SKIP_DETECTOR=0
TRIM=$(echo -n "$USER_MSG" | tr -d '[:space:]')
if [ ${#TRIM} -lt 8 ]; then
  SKIP_DETECTOR=1
fi
case "$(echo "$USER_MSG" | tr '[:upper:]' '[:lower:]' | tr -d '[:punct:]' | xargs)" in
  yes|yeah|yep|ok|okay|sure|thanks|thx|nope|no|cool|noted|got\ it|done|approve|approved|reject|rejected|send\ it|let\ us\ go|lets\ go|cancel|abort|stop)
    SKIP_DETECTOR=1
    ;;
esac

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs/signal-detector}"
mkdir -p "$LOG_DIR"
TS=$(date -Iseconds)
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [ "$SKIP_DETECTOR" -eq 0 ]; then
  # Bug I fix (2026-05-26): retire the signal-detector session if it has
  # grown past RETIRE_AT_BYTES. The signal-detector is conceptually
  # stateless — it writes findings to brain pages, not to its own
  # session. Letting the session grow unbounded breaks compaction past
  # Kimi K2.6's 256k context (probe v9 found the session at 2.95MB /
  # ~738k tokens, broken for 20+ hours, burning ~$3/day in failed
  # compaction attempts).
  #
  # Retire at ~50k tokens (200KB at ~4 bytes/token). Brain captures
  # from prior sessions live in the brain itself; nothing is lost.
  # Openclaw recreates the session jsonl on next `openclaw agent`
  # invocation, so the rename is the entire fix.
  RETIRE_AT_BYTES="${SD_RETIRE_AT_BYTES:-200000}"
  SD_SESSIONS="$HOME/.openclaw/agents/signal-detector/sessions"
  if [ -d "$SD_SESSIONS" ]; then
    for s in "$SD_SESSIONS"/*.jsonl; do
      [ -f "$s" ] || continue
      # Skip already-retired and trajectory files
      case "$s" in *.reset.*|*.trajectory.*) continue;; esac
      size=$(stat -c %s "$s" 2>/dev/null || stat -f %z "$s" 2>/dev/null || echo 0)
      if [ "$size" -gt "$RETIRE_AT_BYTES" ]; then
        rts=$(date -u +%Y%m%dT%H%M%S)
        mv "$s" "${s}.reset.${rts}.bak" 2>/dev/null || true
        # Also retire the matching trajectory file if it exists, so the
        # audit log doesn't dangle pointing at a non-existent session.
        traj="${s%.jsonl}.trajectory.jsonl"
        [ -f "$traj" ] && mv "$traj" "${traj}.reset.${rts}.bak" 2>/dev/null || true
        echo "[$TS] retired oversized signal-detector session $(basename "$s") (${size}B > ${RETIRE_AT_BYTES}B)" >> "$LOG"
      fi
    done
  fi

  echo "[$TS] spawning capture for: ${USER_MSG:0:120}" >> "$LOG"
  # Background detached run — main agent's exec returns immediately.
  # nohup + & + redirect = fire-and-forget.
  nohup openclaw agent \
    --agent signal-detector \
    --message "$USER_MSG" \
    --json \
    >> "$LOG" 2>&1 &
  echo "ok: signal-detector spawned (pid $!, log $LOG)"
else
  echo "ok: signal-detector skipped (operational reply — running router only)"
fi

# ── Intent-router (synchronous) ────────────────────────────────
# Run the deterministic intent classifier and append its output
# so the agent's FIRST tool call returns the routing directive.
# This avoids relying on the agent to fire a second tool call
# (Rule 0.5 in AGENTS.md was being skipped in practice).
echo
echo "─── INTENT ROUTER OUTPUT (binding directive — follow verbatim) ───"
if [ -x "$HOME/bin/intent-router.sh" ]; then
  "$HOME/bin/intent-router.sh" "$USER_MSG"
else
  echo "INTENT=chat"
  echo "REASON=router_not_installed"
fi
