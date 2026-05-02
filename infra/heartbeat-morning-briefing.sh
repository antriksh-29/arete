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
PROMPT="Run my morning briefing. Compose this as ONE final reply, not multiple intermediate messages.

1. Today's calendar — pull from both my Google Calendar accounts via clawvisor MCP (clawvisor__calendar_list_events).
2. For each meeting, do a brain lookup on the attendees (gbrain__query) and surface open threads / recent context.
3. Open threads from the brain — find people I owe a follow-up to where the brain page says no movement for 3+ days.
   IMPORTANT: For each such person, BEFORE declaring them stalled, run clawvisor__gmail_search with query 'from:<their_email> OR to:<their_email> newer_than:3d' to check for fresh activity. If you find a recent email (sent or received), surface it instead of saying stalled. The brain may be a day or two stale — live Gmail is ground truth for very recent activity.
4. Anything urgent in the inbox this week (from clawvisor__gmail_search 'newer_than:7d category:primary' if useful — focus on actionable items, not noise).

Keep it tight, structured, lands on my phone first thing. One final assistant reply, not a stream of 'let me check' narrations."

openclaw agent \
  --agent main \
  --channel telegram \
  --reply-to telegram:775188536 \
  --deliver \
  --message "$PROMPT" >> "$LOG" 2>&1

echo "[$(date -Iseconds)] morning briefing done" >> "$LOG"
