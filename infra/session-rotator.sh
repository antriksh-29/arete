#!/usr/bin/env bash
# session-rotator.sh — proactively retire OpenClaw agent sessions before
# they exceed the LLM context window.
#
# OBSERVED PATTERN (2026-05-22): every ~2-4 hours, the user's main session
# crosses 150-200 messages, the model's context-overflow precheck fires
# (`Context overflow: estimated context size exceeds safe threshold during
# tool loop`), and the next user turn aborts with an `assistantError`.
# User has no signal — they just see the bot stop responding. They send
# /new repeatedly. The spool-stall-watchdog eventually restarts the
# gateway but the session-rotation has to happen on the user's next turn
# anyway (which they keep retrying without realizing).
#
# Today's overflow events:
#   12:09:58  Context overflow at 184 messages (Kimi K2.6)
#   15:04:31  Context overflow at 216 messages (Kimi K2.6)
#
# FIX: proactively retire sessions when they cross MESSAGE_THRESHOLD
# (default 120, well below the 180+ overflow point). The user gets a
# proactive Telegram notification explaining what happened. Next turn
# starts on a fresh session — no overflow, no aborted tool loops.
#
# Pair with the existing spool-stall-watchdog (reactive — catches stuck
# states) for belt-and-suspenders. This script is the PREVENTIVE half.

set -uo pipefail

SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.openclaw/agents/main/sessions}"
MESSAGE_THRESHOLD="${MESSAGE_THRESHOLD:-120}"
STATE_FILE="${STATE_FILE:-$HOME/.openclaw/.session-rotator-state}"
LOG="${LOG:-/tmp/session-rotator.log}"
COOLDOWN_MIN="${COOLDOWN_MIN:-30}"  # don't rotate the same chat twice in a row
# IDLE_MIN: minimum minutes since last session activity before we'll
# rotate (Bug H, 2026-05-25). When the user is in mid-conversation, the
# session file's mtime is recent — rotating mid-thread is exactly the
# disruption Antriksh reported. Waiting 10 minutes of idle means rotations
# happen at natural conversation breaks, not while typing.
IDLE_MIN="${IDLE_MIN:-10}"
LOCK="${LOCK:-/tmp/session-rotator.lock}"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG" >&2
}

# Single-instance lock
exec 9>"$LOCK"
flock -n 9 || exit 0

if [ ! -d "$SESSIONS_DIR" ]; then
  exit 0
fi

# Cooldown — don't fire repeatedly. Cooldown is per-rotator-run, not per-session,
# so a rotation immediately disables further rotations for COOLDOWN_MIN. This is
# fine because real session growth takes hours.
now_epoch=$(date +%s)
if [ -f "$STATE_FILE" ]; then
  last_fire=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  age_sec=$(( now_epoch - last_fire ))
  if [ "$age_sec" -lt $(( COOLDOWN_MIN * 60 )) ]; then
    exit 0
  fi
fi

# Find the active session — most recently modified .jsonl that's not a
# trajectory/reset/checkpoint variant. For single-tenant (Antriksh's
# personal bot), there's effectively one main session at a time.
active=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null \
         | grep -v -E '\.(trajectory|reset|checkpoint)\.' \
         | head -1)
if [ -z "$active" ] || [ ! -f "$active" ]; then
  exit 0
fi

# Quick exit if the session is too new (started in the last 15 min — give
# the user some buffer for real conversation flow even if it grew quickly).
mtime=$(stat -c %Y "$active" 2>/dev/null || echo 0)
btime=$(stat -c %W "$active" 2>/dev/null || echo "$mtime")
age_min=$(( (now_epoch - btime) / 60 ))
[ "$btime" = "0" ] && age_min=999  # if btime unavailable, assume old
if [ "$age_min" -lt 15 ]; then
  exit 0
fi

# Idle guard (Bug H, 2026-05-25): don't rotate during active conversation.
# If the session file was modified in the last IDLE_MIN minutes, the user
# is likely mid-thread — rotation would yank context out from under them.
# Wait for a natural pause. The script runs on cron every few minutes, so
# it'll catch the rotation the next time the session is idle.
idle_min=$(( (now_epoch - mtime) / 60 ))
if [ "$idle_min" -lt "$IDLE_MIN" ]; then
  exit 0
fi

# Count messages. The session jsonl uses one JSON object per line, each
# with a "type":"message" or similar. We use wc -l for the rough count;
# it's close enough since each turn writes 2-6 lines (user, assistant,
# tool calls, tool results).
line_count=$(wc -l < "$active" 2>/dev/null || echo 0)

if [ "$line_count" -lt "$MESSAGE_THRESHOLD" ]; then
  exit 0
fi

log "session $(basename "$active") has $line_count lines (>= $MESSAGE_THRESHOLD threshold), idle ${idle_min}min — rotating silently"

# Retire by renaming. OpenClaw uses the .reset.<ts>.bak convention for
# retired sessions; the next user message creates a fresh session file.
retire_path="${active}.reset.$(date -u +%Y-%m-%dT%H-%M-%S).bak"
mv "$active" "$retire_path"

# Also retire the matching trajectory file (audit log) so it doesn't
# linger pointing at a non-existent session.
traj="${active%.jsonl}.trajectory.jsonl"
if [ -f "$traj" ]; then
  mv "$traj" "${traj}.reset.$(date -u +%Y-%m-%dT%H-%M-%S).bak"
fi

log "retired: $retire_path"
echo "$now_epoch" > "$STATE_FILE"

# Silent rotation (Bug H, 2026-05-25). The original version Telegram'd
# the user with "🔄 I rotated to a fresh session — your conversation had
# grown to N messages..." which Antriksh reports is the single most
# disruptive bubble — it always appears mid-flow and breaks his thought.
#
# Trade-off: without the notification, the user notices the bot "forgot"
# earlier context and may be briefly confused. Mitigation: the idle guard
# above ensures rotation only fires when the user is NOT actively chatting,
# so by the time they come back the context-loss is less jarring (they
# were paused anyway). They can also ask "recall what we were discussing
# earlier" and the bot can look it up from saved brain pages.

exit 0
