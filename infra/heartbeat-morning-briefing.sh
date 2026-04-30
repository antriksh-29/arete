#!/bin/bash
# Morning briefing heartbeat — runs at 8am IST daily.
# Triggers an agent turn that pulls today's calendar + open threads from the
# brain, composes a briefing, and delivers it to Antriksh's Telegram.

set -e
export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/heartbeat-$(date +%Y-%m-%d).log"

echo "[$(date -Iseconds)] morning briefing start" >> "$LOG"

# The agent has skills (briefing, daily-task-prep) and gbrain MCP. It will
# query calendar via ClawVisor, query brain for context, compose, and the
# --deliver flag pushes the reply through Telegram.
PROMPT="Run my morning briefing. Pull today's calendar events from both my Google Calendar accounts via ClawVisor. For each meeting, do a short brain lookup on the attendees and surface any open threads or recent context that's relevant. End with: any follow-ups I promised people that haven't moved in 3+ days. Keep it tight — this lands on my phone first thing in the morning."

openclaw agent \
  --agent main \
  --channel telegram \
  --reply-to telegram:775188536 \
  --deliver \
  --message "$PROMPT" >> "$LOG" 2>&1

echo "[$(date -Iseconds)] morning briefing done" >> "$LOG"
