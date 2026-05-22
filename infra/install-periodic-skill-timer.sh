#!/usr/bin/env bash
# install-periodic-skill-timer.sh — generate + install a systemd
# user-timer + service unit for one periodic user-skill.
#
# Replaces OpenClaw cron + agentTurn dispatch (which kept misrouting
# periodic fires into the skill-author elicit pipeline because Kimi
# interpreted the slug name in the agentTurn message as a refine
# intent). The systemd timer fires `run-periodic-skill.py <slug>`
# directly — no LLM in the dispatch loop.
#
# Pattern matches `heartbeat-morning-briefing.timer` /
# `brain-collectors.timer` which the project already uses for
# deterministic periodic delivery.
#
# Usage:
#   install-periodic-skill-timer.sh <slug> <cron-expr> [tz]
#
# Example:
#   install-periodic-skill-timer.sh paul-graham-insights "30 11 * * *" Asia/Kolkata
#
# This installs:
#   ~/.config/systemd/user/skill-<slug>.timer       (OnCalendar schedule)
#   ~/.config/systemd/user/skill-<slug>.service     (runs the dispatcher)
# enables both, and starts the timer.
#
# Idempotent: re-installing with new schedule rewrites the .timer file
# and restarts the timer unit; the service is unchanged.

set -uo pipefail

SLUG="${1:?slug required}"
CRON_EXPR="${2:?cron expression required (e.g. '30 11 * * *')}"
TZ_NAME="${3:-Asia/Kolkata}"

# Validate cron expression — only the 5-field daily/weekly/hourly forms
# we support get a clean OnCalendar mapping. For weird custom crons,
# fall back to systemd's OnCalendar=<expr> with the same string and
# pray it parses (systemd supports a different syntax than 5-field cron
# but accepts many common cases).
# `parts=($CRON_EXPR)` glob-expands the `*` characters against the cwd.
# Use `read -ra` to split on whitespace WITHOUT globbing.
read -ra parts <<< "$CRON_EXPR"
if [ ${#parts[@]} -ne 5 ]; then
  echo "install-periodic-skill-timer: expected 5-field cron, got: '$CRON_EXPR' (${#parts[@]} fields)" >&2
  exit 1
fi
MIN="${parts[0]}"
HR="${parts[1]}"
DOM="${parts[2]}"
MON="${parts[3]}"
DOW="${parts[4]}"

# systemd OnCalendar form is: DayOfWeek YYYY-MM-DD HH:MM:SS
# Common cases:
#   "* * * * *"        -> minutely
#   "M H * * *"        -> daily at H:M
#   "M H * * D"        -> weekly on day D at H:M
#   "M H D * *"        -> monthly on day D at H:M
# For our use cases (mostly daily) we keep it simple.
# systemd OnCalendar requires two-digit zero-padded HH:MM. `12:5:00`
# is interpreted as `12:05:00` quietly but `13:2:00` rounds to next
# day — pad them explicitly.
HR_PAD=$(printf "%02d" "$HR" 2>/dev/null || echo "$HR")
MIN_PAD=$(printf "%02d" "$MIN" 2>/dev/null || echo "$MIN")
if [ "$DOM" = "*" ] && [ "$MON" = "*" ] && [ "$DOW" = "*" ]; then
  ON_CAL="*-*-* ${HR_PAD}:${MIN_PAD}:00 ${TZ_NAME}"
elif [ "$DOM" = "*" ] && [ "$MON" = "*" ]; then
  case "$DOW" in
    0) DAY_NAME="Sun" ;; 1) DAY_NAME="Mon" ;; 2) DAY_NAME="Tue" ;;
    3) DAY_NAME="Wed" ;; 4) DAY_NAME="Thu" ;; 5) DAY_NAME="Fri" ;;
    6) DAY_NAME="Sat" ;; *) DAY_NAME="Mon" ;;
  esac
  ON_CAL="${DAY_NAME} *-*-* ${HR_PAD}:${MIN_PAD}:00 ${TZ_NAME}"
elif [ "$DOW" = "*" ] && [ "$MON" = "*" ]; then
  DOM_PAD=$(printf "%02d" "$DOM" 2>/dev/null || echo "$DOM")
  ON_CAL="*-*-${DOM_PAD} ${HR_PAD}:${MIN_PAD}:00 ${TZ_NAME}"
else
  ON_CAL="*-*-* ${HR_PAD}:${MIN_PAD}:00 ${TZ_NAME}"
fi

CONF_DIR="$HOME/.config/systemd/user"
mkdir -p "$CONF_DIR"

# Service unit — runs the dispatcher
SERVICE_PATH="$CONF_DIR/skill-${SLUG}.service"
cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Periodic skill dispatcher — ${SLUG}
After=openclaw-gateway.service

[Service]
Type=oneshot
Environment=PATH=%h/.bun/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 %h/personal-assistant/infra/run-periodic-skill.py ${SLUG}
TimeoutStartSec=120
StandardOutput=journal
StandardError=journal
EOF

# Timer unit — when to fire
TIMER_PATH="$CONF_DIR/skill-${SLUG}.timer"
cat > "$TIMER_PATH" <<EOF
[Unit]
Description=Schedule for periodic skill ${SLUG} (cron: ${CRON_EXPR} ${TZ_NAME})

[Timer]
OnCalendar=${ON_CAL}
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable "skill-${SLUG}.timer" 2>&1 | tail -1
systemctl --user restart "skill-${SLUG}.timer"

# Show the next fire time
NEXT=$(systemctl --user list-timers --all --no-pager 2>/dev/null \
  | awk -v t="skill-${SLUG}.timer" '$0 ~ t {for(i=1;i<=4;i++) printf "%s ", $i; print ""}' \
  | head -1)
echo "installed timer ${SLUG} (cron=${CRON_EXPR} -> OnCalendar=${ON_CAL})"
echo "next fire: ${NEXT:-unknown}"
