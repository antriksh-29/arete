#!/bin/bash
# Morning briefing heartbeat — runs at 8am IST daily.
#
# Calls the deterministic Python orchestrator (~/bin/daily-brief.py), which
# pulls calendar/weather/tasks/emails/markets/fitness in locked order,
# composes with HTML bold headers, and posts directly to Telegram.
#
# Replaced the agent-based briefing on 2026-05-06: the agent kept
# reordering sections despite explicit prompt instruction. Composition is
# now in Python; the only LLM call is the email triage step (via llm.py).

set -e
export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/heartbeat-$(TZ=Asia/Kolkata date +%Y-%m-%d).log"

echo "[$(date -Iseconds)] morning briefing start" >> "$LOG"

python3 /home/botuser/bin/daily-brief.py >> "$LOG" 2>&1
RC=$?

if [ $RC -eq 0 ]; then
  echo "[$(date -Iseconds)] morning briefing done" >> "$LOG"
else
  echo "[$(date -Iseconds)] morning briefing FAILED rc=$RC" >> "$LOG"
  /home/botuser/bin/send-telegram.sh "⚠️ Morning briefing failed (rc=$RC). Check $LOG on the bot."
  exit $RC
fi
