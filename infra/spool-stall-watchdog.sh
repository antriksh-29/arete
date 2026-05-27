#!/usr/bin/env bash
# spool-stall-watchdog.sh — detect and recover from stuck Telegram inbound.
#
# Observed 2026-05-22: user asked Arete a question that Kimi fabricated
# the answer for, then looped on `cat > /tmp/youtube_subs.txt` for 30+
# minutes. The first message blocked the queue; every subsequent /new
# the user sent stacked behind it in the spool. The user thought the
# whole bot was dead and tried multiple /restart and /new commands —
# each adding more queued work, none clearing the head-of-line block.
#
# This watchdog runs every minute via systemd. If any spool entry is
# older than STALL_THRESHOLD_MIN (default 5 min), it:
#   1. Snapshots the stuck message text (for the user to see)
#   2. Clears the spool
#   3. Retires the active session (move aside, fresh start)
#   4. Restarts the gateway with SIGKILL (skip the 30s drain)
#   5. Sends a Telegram message: "That request got stuck — cleared. Try again."
#
# Trigger criteria:
#   • At least one spool file with mtime older than STALL_THRESHOLD_MIN minutes
#   • OR the spool depth exceeds STALL_DEPTH (default 3) regardless of age
#
# Skip criteria (avoid restart loops):
#   • Gateway was restarted less than COOLDOWN_MIN minutes ago
#   • Watchdog already fired in the last COOLDOWN_MIN minutes
#
# Usage:
#   spool-stall-watchdog.sh           # one-shot check, exit 0 if clean / fixed
#   spool-stall-watchdog.sh --dry-run # report only, don't act

set -uo pipefail

SPOOL_DIR="${SPOOL_DIR:-$HOME/.openclaw/telegram/ingress-spool-default}"
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.openclaw/agents/main/sessions}"
DRAFTS_DIR="${DRAFTS_DIR:-$HOME/personal-assistant/.context/skill-author-drafts}"
STATE_FILE="${STATE_FILE:-$HOME/.openclaw/.spool-watchdog-state}"
LOG="${LOG:-/tmp/spool-stall-watchdog.log}"
# Threshold raised from 5min → 10min after 2026-05-22 incident: a
# legitimate "list all 161 YouTube subs + categorize + brain write"
# request was actively logging journal output but still got killed
# at the 5-min mark. Long-running but progressing work needs room.
STALL_THRESHOLD_MIN="${STALL_THRESHOLD_MIN:-10}"
STALL_DEPTH="${STALL_DEPTH:-3}"
COOLDOWN_MIN="${COOLDOWN_MIN:-3}"
# If the gateway has emitted ANY journal entry in the last
# JOURNAL_QUIET_MIN minutes, treat the job as alive — don't kill it
# even if the spool file is "old". A truly hung gateway emits nothing.
JOURNAL_QUIET_MIN="${JOURNAL_QUIET_MIN:-2}"
TG_SEND="$HOME/bin/send-telegram.sh"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() {
  local msg="[$(date -Iseconds)] $*"
  echo "$msg" | tee -a "$LOG" >&2
}

[ -d "$SPOOL_DIR" ] || { log "spool dir not found: $SPOOL_DIR — nothing to watch"; exit 0; }

# Cooldown check — don't fire repeatedly
now_epoch=$(date +%s)
if [ -f "$STATE_FILE" ]; then
  last_fire=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
  age_sec=$(( now_epoch - last_fire ))
  if [ "$age_sec" -lt $(( COOLDOWN_MIN * 60 )) ]; then
    # within cooldown; silent skip
    exit 0
  fi
fi

# Detect stall: any spool file older than threshold, OR depth above floor
stuck_files=$(find "$SPOOL_DIR" -maxdepth 1 -name '*.json' -mmin +"$STALL_THRESHOLD_MIN" 2>/dev/null | head -10)
total_depth=$(find "$SPOOL_DIR" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l | tr -d ' ')

is_stuck=0
reason=""
if [ -n "$stuck_files" ]; then
  oldest_count=$(echo "$stuck_files" | wc -l | tr -d ' ')
  is_stuck=1
  reason="$oldest_count spool message(s) older than ${STALL_THRESHOLD_MIN} min"
fi
if [ "$total_depth" -ge "$STALL_DEPTH" ]; then
  is_stuck=1
  [ -n "$reason" ] && reason="$reason; depth=${total_depth}" || reason="spool depth ${total_depth} (>= ${STALL_DEPTH})"
fi

if [ "$is_stuck" -eq 0 ]; then
  # Clean state — quiet exit
  exit 0
fi

# Liveness check — if the gateway has emitted ANY journal output in
# the last JOURNAL_QUIET_MIN minutes, the work is progressing. Don't
# kill long-running but active jobs (PR #129's YouTube fetch is the
# motivating case: 5+ min of pagination + categorization, but
# constantly logging activity).
since="${JOURNAL_QUIET_MIN} minutes ago"
recent_journal_lines=$(journalctl --user -u openclaw-gateway \
                        --since "$since" --no-pager 2>/dev/null \
                      | grep -cE "node\[|gateway|agent|tool|model" 2>/dev/null \
                      || echo 0)
if [ "$recent_journal_lines" -ge 3 ]; then
  log "spool old but gateway is active ($recent_journal_lines journal lines in last ${JOURNAL_QUIET_MIN}min) — skipping kill"
  # Update cooldown so we don't check again immediately
  echo "$now_epoch" > "$STATE_FILE"
  exit 0
fi

log "stall detected: $reason (journal quiet: $recent_journal_lines lines in last ${JOURNAL_QUIET_MIN}min)"

# Snapshot the stuck head-of-line message for context
head_msg=""
oldest_spool=$(find "$SPOOL_DIR" -maxdepth 1 -name '*.json' -printf '%T@ %p\n' 2>/dev/null \
              | sort -n | head -1 | awk '{print $2}')
if [ -n "$oldest_spool" ] && [ -f "$oldest_spool" ]; then
  head_msg=$(python3 -c "
import json
try:
    d = json.load(open('$oldest_spool'))
    t = d.get('update', {}).get('message', {}).get('text', '')
    print(t[:200])
except Exception:
    pass
" 2>/dev/null)
fi

if [ "$DRY_RUN" -eq 1 ]; then
  log "DRY RUN — would: clear spool, retire session, SIGKILL+restart gateway"
  log "  head message: ${head_msg:0:120}"
  exit 0
fi

# Cleanup actions
rm -f "$SPOOL_DIR"/*.json
rm -f "$DRAFTS_DIR"/draft-*.json 2>/dev/null

# Retire the live session (most recent jsonl that isn't a trajectory/reset/checkpoint)
sess=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | grep -v trajectory | grep -v reset | grep -v checkpoint | head -1)
if [ -n "$sess" ] && [ -f "$sess" ]; then
  mv "$sess" "${sess}.reset.$(date -u +%Y%m%d-%H%M%S).bak"
  log "retired session: $(basename "$sess")"
fi

# Hard restart — SIGKILL to skip the 30s drain timeout
systemctl --user kill --signal=SIGKILL openclaw-gateway >/dev/null 2>&1
sleep 2
systemctl --user start openclaw-gateway >/dev/null 2>&1
log "gateway restarted (SIGKILL + start)"

# Mark cooldown
echo "$now_epoch" > "$STATE_FILE"

# Notify user on Telegram
if [ -x "$TG_SEND" ]; then
  preview="${head_msg:0:120}"
  [ -n "$preview" ] && preview=" (last message: \"$preview\")"
  "$TG_SEND" "⚠️ Your last request got stuck and I had to clear the queue${preview}. Try sending it again — I'm responsive now." >/dev/null 2>&1 \
    && log "user notified on Telegram" \
    || log "WARN: could not notify user on Telegram (send-telegram.sh missing or failed)"
fi

log "watchdog recovery complete"
exit 0
