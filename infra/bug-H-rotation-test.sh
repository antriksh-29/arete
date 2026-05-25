#!/usr/bin/env bash
# bug-H-rotation-test.sh — non-destructive verification that PR #144's
# silent-rotation fix actually landed on the VPS.
#
# Read-only by default. Has an optional --force flag that performs a
# REAL live rotation (resets your current session) so we can definitively
# confirm no Telegram bubble fires. Use --force when you're ready to
# accept a context reset (your conversation thread will start fresh).
#
# Usage:
#   bash bug-H-rotation-test.sh           # read-only check
#   bash bug-H-rotation-test.sh --force   # invasive: actually rotates

set -uo pipefail
OUT=/tmp/bug-H-rotation-test.md
exec > >(tee "$OUT")

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

ROTATOR="$HOME/bin/session-rotator.sh"
SESSIONS_DIR="$HOME/.openclaw/agents/main/sessions"
LOG=/tmp/session-rotator.log

section() { echo; echo "## $1"; echo; }

echo "# Bug H verification report"
echo
echo "_Generated: $(date -Iseconds) on $(hostname). Mode: $([ $FORCE = 1 ] && echo 'FORCE (will actually rotate)' || echo 'read-only')_"

section "1. Is the deployed session-rotator.sh the PR #144 version?"
if [ ! -f "$ROTATOR" ]; then
  echo "✗ $ROTATOR missing — deploy didn't sync"
  exit 1
fi
echo "File mtime: $(stat -c %y "$ROTATOR" 2>/dev/null || stat -f %Sm "$ROTATOR" 2>/dev/null)"
echo
echo "### Has the Telegram notification been removed? (should be NO matches)"
if grep -nE 'TG_SEND|rotated to a fresh session|conversation had grown to' "$ROTATOR" >/dev/null 2>&1; then
  echo "✗ STILL HAS the notification block — deploy didn't apply PR #144:"
  grep -nE 'TG_SEND|rotated to a fresh session|conversation had grown to' "$ROTATOR" 2>&1 | head -5
else
  echo "✓ No TG_SEND / rotation-phrase references — PR #144 landed"
fi
echo
echo "### Is IDLE_MIN present? (should be a match)"
if grep -nE 'IDLE_MIN' "$ROTATOR" >/dev/null 2>&1; then
  echo "✓ IDLE_MIN guard present:"
  grep -nE 'IDLE_MIN' "$ROTATOR" | head -5
else
  echo "✗ IDLE_MIN guard missing — deploy didn't apply PR #144"
fi

section "2. Current active session state"
if [ ! -d "$SESSIONS_DIR" ]; then
  echo "(sessions dir missing — bot probably hasn't started yet)"
else
  active=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | grep -v -E '\.(trajectory|reset|checkpoint)\.' | head -1)
  if [ -z "$active" ]; then
    echo "(no active session file)"
  else
    line_count=$(wc -l < "$active" 2>/dev/null || echo 0)
    mtime=$(stat -c %Y "$active" 2>/dev/null || stat -f %m "$active" 2>/dev/null || echo 0)
    now=$(date +%s)
    idle_min=$(( (now - mtime) / 60 ))
    echo "Active session: $(basename "$active")"
    echo "  Line count: $line_count (threshold to rotate: 120)"
    echo "  Idle min:   $idle_min (idle guard: must be >= 10)"
    echo
    if [ "$line_count" -ge 120 ] && [ "$idle_min" -ge 10 ]; then
      echo "✓ Eligible for rotation on next cron tick."
    elif [ "$line_count" -lt 120 ]; then
      echo "ℹ️  Below threshold, no rotation needed yet."
    else
      echo "⏳ Above threshold BUT active (idle < 10 min). Rotation will fire after another $((10 - idle_min)) min of idle."
    fi
  fi
fi

section "3. Recent session-rotator log events"
if [ -f "$LOG" ]; then
  echo "Last 15 lines of $LOG:"
  tail -15 "$LOG"
else
  echo "(no log at $LOG yet — rotator hasn't fired since the log was last cleared)"
fi
echo
echo "### Search for 'user notified' lines (would mean old version still firing somewhere):"
grep -c 'user notified' "$LOG" 2>/dev/null || echo "0"
echo "### Search for 'rotating silently' lines (new wording from PR #144):"
grep -c 'rotating silently' "$LOG" 2>/dev/null || echo "0"

section "4. Recent rotation .reset.bak files (proves rotations are happening)"
ls -lt "$SESSIONS_DIR"/*.reset.*.bak 2>/dev/null | head -5 || echo "(none — no recent rotations)"

section "5. Telegram outbound for the rotation phrase (should be NONE)"
echo "Grep journalctl for any recent Telegram outbound containing the rotation phrase..."
journalctl --user --since '1 hour ago' --no-pager 2>/dev/null | \
  grep -iE 'rotated to a fresh session|conversation had grown to' | \
  head -5 || echo "(none — clean)"

if [ "$FORCE" = 1 ]; then
  section "6. FORCE MODE: invoke the rotator with threshold=1 (one-shot)"
  echo "About to run: MESSAGE_THRESHOLD=1 IDLE_MIN=0 COOLDOWN_MIN=0 bash $ROTATOR"
  echo "This WILL rotate your current session if one exists. Telegram should stay silent."
  echo
  echo "--- script output ---"
  MESSAGE_THRESHOLD=1 IDLE_MIN=0 COOLDOWN_MIN=0 bash "$ROTATOR" 2>&1
  echo "--- end ---"
  echo
  echo "Check Telegram now — there should be NO new bubble from Alfred."
  echo
  echo "### Post-force log:"
  tail -10 "$LOG" 2>/dev/null
  echo
  echo "### Post-force .reset.bak files:"
  ls -lt "$SESSIONS_DIR"/*.reset.*.bak 2>/dev/null | head -3
fi

echo
echo "## Done"
echo "Report saved to: $OUT"
