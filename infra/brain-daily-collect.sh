#!/bin/bash
# Daily Phase 6 collector: pulls last 2d Gmail + last 14d Calendar from
# both accounts via ClawVisor, writes raw JSON + digests under
# ${BRAIN_PATH}-bootstrap-raw/, then commits any changed pages and runs gbrain sync.
#
# 2d Gmail window has intentional overlap with previous day (idempotent — same
# message IDs dedupe in gbrain).
# 14d Calendar window catches updates to events that may have been
# rescheduled or had their attendee list change.
#
# Run via systemd user timer (see brain-collectors.timer).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COLLECTORS_DIR="${COLLECTORS_DIR:-$HOME/collectors}"
RAW_DIR="${RAW_DIR:-$HOME/.brain-bootstrap-raw}"
LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs}"
STAMP=$(date +%Y-%m-%d)
LOG="$LOG_DIR/collect-$STAMP.log"

mkdir -p "$RAW_DIR/email" "$RAW_DIR/calendar" "$LOG_DIR"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

log "=== brain-daily-collect start ==="

# Load secrets from vault (same pattern as gbrain-mcp-wrapper.sh — avoids
# sourcing /etc/arete-secrets/gateway.env which corrupts $-bearing passwords).
# Post-2026-05-16: Composio is the Gmail/Calendar source. ClawVisor env
# vars retired (collectors no longer read them; composio_client picks up
# COMPOSIO_API_KEY itself via the vault).
export PATH="${STATE_HOME}/.bun/bin:$PATH"
export OPENAI_API_KEY="$(/usr/local/bin/arete-cred get OPENAI_API_KEY 2>/dev/null)"
export ANTHROPIC_API_KEY="$(/usr/local/bin/arete-cred get ANTHROPIC_API_KEY 2>/dev/null)"

if ! /usr/local/bin/arete-cred list 2>/dev/null | grep -q "^COMPOSIO_API_KEY "; then
  log "ABORT: COMPOSIO_API_KEY missing from vault (composio Gmail/Calendar required since 2026-05-16 migration)"
  exit 1
fi

OUT_DIR="$RAW_DIR/email" node "$COLLECTORS_DIR/email-collector.mjs" 2 2>&1 | tee -a "$LOG"
OUT_DIR="$RAW_DIR/calendar" node "$COLLECTORS_DIR/calendar-collector.mjs" 14 2>&1 | tee -a "$LOG"

log "collectors finished — running enrichment for new entities (LIMIT=10 per type)"
# Daily incremental enrichment uses GPT-5.4-mini — single-page generation
# (not multi-step), schema well-trodden after Phase 4.5 one-shot. Cost is
# ~$0.001/page vs ~$0.008 on Haiku. If quality regresses (malformed pages,
# bad citations), revert ENRICH_MODEL to claude-haiku-4-5.
ENRICH_LIMIT=10 ENRICH_MODEL=gpt-5.4-mini \
  RAW_EMAIL_DIR="$RAW_DIR/email" RAW_CAL_DIR="$RAW_DIR/calendar" \
  BRAIN_DIR="$HOME/.brain" \
  node "$COLLECTORS_DIR/bootstrap-enrich.mjs" 2>&1 | tee -a "$LOG"

cd "$HOME/.brain"
if [ -n "$(git status --porcelain)" ]; then
  log "brain changes detected — committing + syncing"
  git add -A
  git -c user.email='arete-bot@${PLATFORM_ROOT_DOMAIN}' -c user.name='Arete' \
    commit -m "feat(daily): $(date +%Y-%m-%d) collector run" 2>&1 | tee -a "$LOG"
  gbrain sync --repo "$HOME/.brain" 2>&1 | tee -a "$LOG"
  git push origin main 2>&1 | tee -a "$LOG" || log "push failed (non-fatal — try again next run)"
else
  log "no brain changes today"
fi

log "=== brain-daily-collect done ==="
# Retention: keep last 14 logs
ls -t "$LOG_DIR"/collect-*.log 2>/dev/null | tail -n +15 | xargs -r rm
