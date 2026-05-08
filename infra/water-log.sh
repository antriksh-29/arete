#!/bin/bash
# Water intake logger
#
# Subcommands:
#   log <amount>         — parse and append amount to today's IST log
#   summary [YYYY-MM-DD] — Telegram-formatted summary for a date (default: yesterday IST)
#   total [YYYY-MM-DD]   — total ml for a date to stdout (default: today IST)
#
# Storage (dual):
#   - Filesystem (raw events): ~/.brain-bootstrap-raw/state/water/YYYY-MM-DD.log
#     Each line: "<YYYY-MM-DD HH:MM IST> <ml>"
#   - Brain (compiled view): ~/.brain/habits/water-intake.md
#     Updated on every log call; queryable by brain-first lookups.

set -euo pipefail

STATE_DIR="$HOME/.brain-bootstrap-raw/state/water"
BRAIN_PAGE="$HOME/.brain/habits/water-intake.md"
TARGET_ML=3000

now_ist() { TZ=Asia/Kolkata date "+$1"; }
yesterday_ist() { TZ=Asia/Kolkata date -d 'yesterday' "+$1"; }

mkdir -p "$STATE_DIR" "$(dirname "$BRAIN_PAGE")"

parse_ml() {
  local raw="$1"
  local norm
  norm=$(echo "$raw" | tr '[:upper:]' '[:lower:]' | sed 's/ //g')
  if [[ "$norm" =~ ^([0-9]+\.?[0-9]*)l$ ]]; then
    echo "$(python3 -c "print(int(float('${BASH_REMATCH[1]}') * 1000))")"
  elif [[ "$norm" =~ ^([0-9]+\.?[0-9]*)ml$ ]]; then
    echo "${BASH_REMATCH[1]%.*}"
  else
    echo ""
  fi
}

log_intake() {
  local raw="$1"
  local DATE_ARG="${2:-}"
  local TIME_ARG="${3:-}"
  local ML
  ML=$(parse_ml "$raw")
  if [ -z "$ML" ] || [ "$ML" -le 0 ] 2>/dev/null; then
    echo "Could not parse amount: $raw" >&2
    exit 1
  fi
  local DATE TS LOG_FILE
  if [ -n "$DATE_ARG" ]; then
    if ! [[ "$DATE_ARG" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || ! date -d "$DATE_ARG" >/dev/null 2>&1; then
      echo "Bad date: $DATE_ARG (expected valid YYYY-MM-DD)" >&2
      exit 1
    fi
    # Reject future dates — backfill is for past only.
    if [ "$DATE_ARG" \> "$(now_ist '%Y-%m-%d')" ]; then
      echo "Refusing to log future date: $DATE_ARG" >&2
      exit 1
    fi
    DATE="$DATE_ARG"
    # Default backfill time = 12:00 IST so day-summary entries look sensible.
    local T="${TIME_ARG:-12:00}"
    if ! [[ "$T" =~ ^[0-9]{2}:[0-9]{2}$ ]]; then
      echo "Bad time: $T (expected HH:MM)" >&2
      exit 1
    fi
    TS="$DATE $T IST"
  else
    DATE=$(now_ist '%Y-%m-%d')
    TS=$(now_ist '%Y-%m-%d %H:%M IST')
  fi
  LOG_FILE="$STATE_DIR/$DATE.log"
  echo "$TS $ML" >> "$LOG_FILE"
  echo "$ML"
}

total_for_date() {
  local DATE="$1"
  local LOG_FILE="$STATE_DIR/$DATE.log"
  if [ ! -f "$LOG_FILE" ]; then
    echo 0; return
  fi
  awk '{sum += $4} END {print sum+0}' "$LOG_FILE"
}

entries_for_date() {
  local DATE="$1"
  local LOG_FILE="$STATE_DIR/$DATE.log"
  if [ ! -f "$LOG_FILE" ]; then echo ""; return; fi
  awk '{
    ml = $4
    if (ml >= 1000 && ml % 1000 == 0) printf "%dL ", ml/1000
    else if (ml >= 1000) printf "%.1fL ", ml/1000
    else printf "%dml ", ml
  }' "$LOG_FILE" | sed "s/ $//"
}

# Recompute the brain page from filesystem state. Idempotent — safe to call
# repeatedly. Reads up to last 30 days, computes 7-day and overall stats,
# stamps full markdown back to ~/.brain/habits/water-intake.md.
rebuild_brain_page() {
  python3 - "$STATE_DIR" "$BRAIN_PAGE" "$TARGET_ML" <<'PYEOF'
import os, sys, datetime, re
state_dir, page_path, target_ml_str = sys.argv[1], sys.argv[2], sys.argv[3]
target_ml = int(target_ml_str)

today = datetime.date.today()
# Build dict of date -> (entries, total_ml)
day_data = {}
for fname in sorted(os.listdir(state_dir)) if os.path.isdir(state_dir) else []:
    if not fname.endswith('.log'): continue
    date_str = fname[:-4]
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        continue
    entries = []
    total = 0
    with open(os.path.join(state_dir, fname)) as f:
        for line in f:
            parts = line.split()
            # Format: "YYYY-MM-DD HH:MM IST <ml>"
            if len(parts) >= 4:
                try:
                    ml = int(parts[3])
                    entries.append({'time': parts[1], 'ml': ml})
                    total += ml
                except ValueError:
                    continue
    day_data[d] = {'entries': entries, 'total': total}

def fmt_ml(ml):
    if ml >= 1000 and ml % 1000 == 0: return f"{ml//1000}L"
    if ml >= 1000: return f"{ml/1000:.2f}L"
    return f"{ml}ml"

# Stats
last_7_days = sorted([d for d in day_data if (today - d).days < 7], reverse=True)
hit_target_7 = sum(1 for d in last_7_days if day_data[d]['total'] >= target_ml)
total_7 = sum(day_data[d]['total'] for d in last_7_days)
all_days = sorted(day_data.keys(), reverse=True)
hit_target_all = sum(1 for d in all_days if day_data[d]['total'] >= target_ml)
total_all = sum(day_data[d]['total'] for d in all_days)

today_total = day_data.get(today, {}).get('total', 0)
today_entries = day_data.get(today, {}).get('entries', [])

# Compose markdown
lines = [
    "---",
    "type: habit",
    "name: Water intake",
    "aliases: [hydration, water-log]",
    f"created: {min(all_days).isoformat() if all_days else today.isoformat()}",
    f"updated: {today.isoformat()}",
    "tags: [habits, hydration, daily]",
    "---",
    "",
    "# Water intake",
    "",
    "Daily water intake tracking. Target: 3L/day. Tracker: `~/bin/water-log.sh`.",
    "Logs accrue throughout the day; this page is recomputed on every log.",
    "",
    "## Today",
    "",
    f"**Total:** {fmt_ml(today_total)} / 3L target ({round(100*today_total/target_ml)}%)",
    "",
]
if today_entries:
    lines.append("Entries:")
    for e in today_entries:
        lines.append(f"- {e['time']} — {fmt_ml(e['ml'])}")
else:
    lines.append("No entries yet today.")
lines.append("")

# Last 7 days table
lines.append("## Last 7 days")
lines.append("")
lines.append("| Date | Total | % of target | Hit |")
lines.append("|------|-------|-------------|-----|")
for d in last_7_days:
    t = day_data[d]['total']
    pct = round(100 * t / target_ml)
    hit = "✅" if t >= target_ml else "—"
    lines.append(f"| {d.isoformat()} | {fmt_ml(t)} | {pct}% | {hit} |")
lines.append("")

# Stats
days_logged = len(all_days)
days_logged_7 = len(last_7_days)
lines.append("## Stats")
lines.append("")
lines.append(f"- **Last 7 days:** hit target {hit_target_7}/{days_logged_7 or 0} days · total {fmt_ml(total_7)} consumed")
lines.append(f"- **Overall (since start):** hit target {hit_target_all}/{days_logged} days · total {fmt_ml(total_all)} consumed")
lines.append(f"- **Days tracked:** {days_logged}")
lines.append(f"- **Last updated:** {today.isoformat()}")

with open(page_path, "w") as f:
    f.write("\n".join(lines) + "\n")
PYEOF
}

commit_brain() {
  cd "$HOME/.brain" 2>/dev/null || return 0
  if [ -n "$(git status --porcelain habits/water-intake.md 2>/dev/null)" ]; then
    git add habits/water-intake.md
    git -c user.email='antriksh.bot@getarete.co' -c user.name='Alfred' \
      commit -m "habit: water-intake update $(now_ist '%Y-%m-%d %H:%M')" >/dev/null 2>&1 || true
  fi
}

build_summary_msg() {
  local DATE="$1"
  local DISPLAY_DATE TOTAL_ML ENTRIES TOTAL_L SHORT_ML SHORT_L STATUS
  DISPLAY_DATE=$(TZ=Asia/Kolkata date -d "$DATE" '+%a %d %b')
  TOTAL_ML=$(total_for_date "$DATE")
  ENTRIES=$(entries_for_date "$DATE")
  TOTAL_L=$(python3 -c "t=$TOTAL_ML; print(f'{t/1000:.2f}L' if t%1000 else f'{t//1000}L')")
  if [ "$TOTAL_ML" -ge "$TARGET_ML" ]; then
    STATUS="✅ Target met!"
  else
    SHORT_ML=$(( TARGET_ML - TOTAL_ML ))
    SHORT_L=$(python3 -c "s=$SHORT_ML; print(f'{s/1000:.2f}L' if s%1000 else f'{s//1000}L')")
    STATUS="❌ Target not met (${SHORT_L} short)"
  fi
  if [ -z "$ENTRIES" ]; then
    printf '💧 Water log — %s\n\nNo entries recorded.\n\nTarget: 3L — ❌ Not met\n' "$DISPLAY_DATE"
  else
    printf '💧 Water log — %s\n\nTotal: %s / 3L target\n%s\n\nEntries: %s\n' "$DISPLAY_DATE" "$TOTAL_L" "$STATUS" "$ENTRIES"
  fi
}

# Pure stdout version — agent reads and decides format. NO Telegram side-effect.
print_summary() { build_summary_msg "$1"; }

# Cron-only path: composes verbose msg and sends to Telegram. Used by
# water-summary.timer at 6am IST. Agent flows MUST NOT call this — they
# use `summary` (stdout) + `range` (compact list) and compose their own reply.
notify_summary() {
  local DATE="$1"
  local MSG
  MSG=$(build_summary_msg "$DATE")
  /home/botuser/bin/send-telegram.sh "$MSG"
}

# Compact one-line-per-day list for any [from] [to] inclusive range.
# Same shape as `week`. Stdout only.
range_summary() {
  local FROM="$1" TO="$2"
  if ! [[ "$FROM" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || ! [[ "$TO" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "Usage: water-log.sh range <YYYY-MM-DD> <YYYY-MM-DD>" >&2
    return 1
  fi
  python3 - "$STATE_DIR" "$TARGET_ML" "$FROM" "$TO" <<'PYEOF'
import os, sys, datetime
state_dir, target = sys.argv[1], int(sys.argv[2])
d_from = datetime.date.fromisoformat(sys.argv[3])
d_to = datetime.date.fromisoformat(sys.argv[4])
if d_to < d_from:
    print("range end before start", file=sys.stderr); sys.exit(1)
days = []
d = d_from
while d <= d_to:
    days.append(d); d += datetime.timedelta(days=1)
def total(d):
    f = os.path.join(state_dir, f"{d.isoformat()}.log")
    if not os.path.exists(f): return 0
    s = 0
    with open(f) as fh:
        for line in fh:
            p = line.split()
            if len(p) >= 4:
                try: s += int(p[3])
                except: pass
    return s
def fmt(ml):
    if ml >= 1000 and ml % 1000 == 0: return f"{ml//1000}L"
    if ml >= 1000: return f"{ml/1000:.2f}L"
    return f"{ml}ml"
print(f"💧 Water — {d_from.strftime('%d %b')} → {d_to.strftime('%d %b')}")
print()
hit = 0; total_ml = 0
for d in days:
    t = total(d)
    total_ml += t
    pct = round(100 * t / target)
    mark = "✅" if t >= target else ("➖" if t == 0 else "❌")
    if t >= target: hit += 1
    print(f"{mark} {d.strftime('%a %d %b')}: {fmt(t)} ({pct}%)")
print()
print(f"Hit target: {hit}/{len(days)} days · total {fmt(total_ml)}")
PYEOF
}

case "${1:-}" in
  --smoke)
    # Cold-boot callability check — no state writes, no network.
    echo '{"ok":true,"skill":"water-log","user_message":"water-log smoke ok"}'
    exit 0
    ;;
  log)
    ML=$(log_intake "$2" "${3:-}" "${4:-}")
    TARGET_DATE="${3:-$(now_ist '%Y-%m-%d')}"
    TOTAL=$(total_for_date "$TARGET_DATE")
    TOTAL_L=$(python3 -c "t=$TOTAL; print(f'{t/1000:.2f}L' if t%1000 else f'{t//1000}L')")
    rebuild_brain_page
    commit_brain
    if [ "$TARGET_DATE" = "$(now_ist '%Y-%m-%d')" ]; then
      echo "Logged ${ML}ml. Today so far: $TOTAL_L / 3L"
    else
      echo "Backfilled ${ML}ml on $TARGET_DATE. Day total: $TOTAL_L / 3L"
    fi
    ;;
  summary)
    # Stdout only. Agent reads this and composes its own Telegram reply.
    DATE="${2:-$(yesterday_ist '%Y-%m-%d')}"
    rebuild_brain_page
    commit_brain
    print_summary "$DATE"
    ;;
  notify)
    # Cron-only: composes + sends verbose Telegram message. DO NOT call
    # from agent flows.
    DATE="${2:-$(yesterday_ist '%Y-%m-%d')}"
    rebuild_brain_page
    commit_brain
    notify_summary "$DATE"
    ;;
  range)
    rebuild_brain_page
    commit_brain
    range_summary "${2:-}" "${3:-}"
    ;;
  total)
    DATE="${2:-$(now_ist '%Y-%m-%d')}"
    total_for_date "$DATE"
    ;;
  week)
    # 7-day review: prints a Telegram-friendly summary of the last 7 days
    # ending today IST. Useful for weekly check-ins after backfill.
    rebuild_brain_page
    commit_brain
    python3 - "$STATE_DIR" "$TARGET_ML" <<'PYEOF'
import os, sys, datetime
state_dir, target = sys.argv[1], int(sys.argv[2])
today = datetime.date.today()
days = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
def total(d):
    f = os.path.join(state_dir, f"{d.isoformat()}.log")
    if not os.path.exists(f): return 0
    s = 0
    with open(f) as fh:
        for line in fh:
            p = line.split()
            if len(p) >= 4:
                try: s += int(p[3])
                except: pass
    return s
def fmt(ml):
    if ml >= 1000 and ml % 1000 == 0: return f"{ml//1000}L"
    if ml >= 1000: return f"{ml/1000:.2f}L"
    return f"{ml}ml"
print(f"💧 Water — last 7 days")
print()
hit = 0; total_ml = 0
for d in days:
    t = total(d)
    total_ml += t
    pct = round(100 * t / target)
    mark = "✅" if t >= target else ("➖" if t == 0 else "❌")
    if t >= target: hit += 1
    label = d.strftime("%a %d %b")
    print(f"{mark} {label}: {fmt(t)} ({pct}%)")
print()
print(f"Hit target: {hit}/7 days · total {fmt(total_ml)}")
PYEOF
    ;;
  rebuild-brain)
    # Manual rebuild (also useful for backfill / migration)
    rebuild_brain_page
    commit_brain
    echo "Rebuilt $BRAIN_PAGE"
    ;;
  *)
    echo "Usage: water-log.sh {log <amount> [YYYY-MM-DD] [HH:MM]|summary [date]|range <from> <to>|week|notify [date]|total [date]|rebuild-brain}" >&2
    exit 1
    ;;
esac
