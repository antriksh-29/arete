#!/bin/bash
# Meeting-prep heartbeat — runs every 15 min during work hours (8am-10pm IST).
# For each calendar event starting in the next 25-40 min that we haven't
# already sent prep for, triggers an agent turn that writes prep notes
# (attendee context, recent threads, open items) and delivers to Telegram.
#
# Idempotency: $STATE_DIR/meeting-prep-sent.txt holds event IDs we've notified
# on. Cleared automatically: rotated daily.

set -e
export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STATE_DIR="${STATE_DIR:-$HOME/.brain-bootstrap-raw/state}"
LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs}"
mkdir -p "$STATE_DIR" "$LOG_DIR"
TODAY_IST=$(TZ=Asia/Kolkata date +%Y-%m-%d)
SENT_FILE="$STATE_DIR/meeting-prep-sent-$TODAY_IST.txt"
touch "$SENT_FILE"
LOG="$LOG_DIR/heartbeat-$TODAY_IST.log"

CLAWVISOR_URL=$(/usr/local/bin/bot-cred get CLAWVISOR_URL)
TOKEN=$(/usr/local/bin/bot-cred get CLAWVISOR_AGENT_TOKEN)
TASK=$(/usr/local/bin/bot-cred get CLAWVISOR_STANDING_TASK_ID)

# Fetch upcoming events from both calendars (next 1 hour window).
FROM=$(date -u -d "+0 minutes" +%Y-%m-%dT%H:%M:%SZ)
TO=$(date -u -d "+1 hour" +%Y-%m-%dT%H:%M:%SZ)

for ACCOUNT in antriksh1980@gmail.com antriksh@getarete.co; do
  SID=$(uuidgen)
  RESP=$(curl -s -X POST "$CLAWVISOR_URL/api/gateway/request?wait=true" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"task_id\":\"$TASK\",\"session_id\":\"$SID\",\"service\":\"google.calendar:$ACCOUNT\",\"action\":\"list_events\",\"params\":{\"from\":\"$FROM\",\"to\":\"$TO\",\"max_results\":10,\"single_events\":true,\"order_by\":\"startTime\"},\"reason\":\"Heartbeat meeting-prep: list events starting in the next hour from $ACCOUNT to identify which need a 30-min-out prep notification\"}")

  echo "$RESP" | python3 -c "
import json, sys, datetime
data = json.load(sys.stdin)
events = data.get('result', {}).get('data', [])
now = datetime.datetime.now(datetime.timezone.utc)
for ev in events:
    start = ev.get('start', '')
    if not start: continue
    # Parse start (handle both Z-suffix and +HH:MM offset formats)
    try:
        dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
    except: continue
    minutes_out = int((dt - now).total_seconds() / 60)
    # Window: 25-40 min before start
    if 25 <= minutes_out <= 40:
        print(f'{ev[\"id\"]}|{minutes_out}|{ev.get(\"summary\",\"(untitled)\")}|$ACCOUNT')
" 2>>"$LOG" | while IFS='|' read -r EVID MIN SUMMARY ACCT; do
    if grep -qxF -- "$EVID" "$SENT_FILE"; then
      continue
    fi
    echo "[$(date -Iseconds)] sending prep for: $SUMMARY (in ${MIN}min, $ACCT, id=$EVID)" >> "$LOG"
    PROMPT="A meeting starts in ${MIN} minutes: \"$SUMMARY\" (calendar account: $ACCT, event id: $EVID). Pull the event details and attendees via clawvisor MCP. For the non-self attendees, query the brain (gbrain__query) for context — recent threads, open follow-ups, what we last talked about. Compose a short prep message (under 8 lines).

MANDATORY brain write-back (per identity/BRAIN_PROPAGATION.md): for EVERY new insight you derive about an attendee from live data (e.g. recent Gmail thread you found, schedule conflict you noticed), call gbrain__add_timeline_entry on that person's brain page BEFORE sending the prep. This keeps the brain fresh so future preps and briefings build on it instead of rediscovering.

Land the user-facing prep message on Telegram. Do brain writes silently."
    openclaw agent \
      --agent main \
      --channel telegram \
      --reply-to telegram:775188536 \
      --deliver \
      --message "$PROMPT" >> "$LOG" 2>&1
    echo "$EVID" >> "$SENT_FILE"
  done
done

# Daily rotation: keep last 7 sent-files
ls -t "$STATE_DIR"/meeting-prep-sent-*.txt 2>/dev/null | tail -n +8 | xargs -r rm
ls -t "$LOG_DIR"/heartbeat-*.log 2>/dev/null | tail -n +14 | xargs -r rm
