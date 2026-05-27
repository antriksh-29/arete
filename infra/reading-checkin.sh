#!/bin/bash
# Daily reading habit check-in.
#
# Subcommands:
#   ping [--for YYYY-MM-DD]   send the morning Telegram check-in for
#                             yesterday's reading. With --for, ask about
#                             a specific past date instead (used for
#                             backfill-style testing and missed-day pings).
#   log yes|no [YYYY-MM-DD]   record a result (date defaults to yesterday IST)
#   stats                     print current stats to stdout
#   verify                    list any missing dates from start through yesterday
#   --smoke                   cold-boot callability check
#
# Backing store: ${BRAIN_PATH}/habits/book-reading.md (committed to brain repo
# by the daily collector + on every log update). The page has a markdown
# log table; this script appends rows + recomputes the stats block.
#
# Discipline (added 2026-05-10 after silent-failure incident):
#   - set -uo pipefail (was: just -e). Unset vars + pipe failures now error.
#   - ERR trap logs the failing line+command to journal so a silent abort
#     never happens again — previous version exited with no stdout/stderr
#     on May 6/7/8.
#   - format_ping checks the captured stats are non-empty before sending
#     (prevents shipping a malformed Telegram message).

set -uo pipefail

# Loud-fail trap. journalctl picks up stderr from systemd; this guarantees
# a one-line "FAIL: <cmd> at line <N>" record on every silent abort.
trap 'rc=$?; echo "[reading-checkin] FAIL rc=$rc at line ${LINENO} cmd: ${BASH_COMMAND}" >&2' ERR

BRAIN_PAGE="$HOME/.brain/habits/book-reading.md"
START_DATE="2026-05-01"

now_ist() { TZ=Asia/Kolkata date "+$1"; }
yesterday_ist() { TZ=Asia/Kolkata date -d 'yesterday' "+$1"; }

ensure_page() {
  if [ ! -f "$BRAIN_PAGE" ]; then
    mkdir -p "$(dirname "$BRAIN_PAGE")"
    cat > "$BRAIN_PAGE" <<EOF
---
type: habit
title: Book Reading
created: $START_DATE
updated: $(now_ist '%Y-%m-%d')
tags: [habits, reading, daily]
---

# Book Reading

Daily reading habit. Started $START_DATE. Daily check-in at 10am IST asks
about yesterday's reading; reply yes or no on Telegram.

## Log

| Date | Read |
|------|------|

## Stats

(computed by ~/bin/reading-checkin.sh on each ping/log)

EOF
  fi
}

# Add or update a single date's entry. Idempotent on date.
log_entry() {
  local DATE="$1"
  local VALUE="$2"  # yes|no
  ensure_page

  # Normalize value
  case "$VALUE" in
    y|yes|Y|YES|done|Done) VALUE="yes" ;;
    n|no|N|NO|skipped|Skipped) VALUE="no" ;;
    *) echo "log: invalid value '$VALUE' (need yes|no)" >&2; exit 1 ;;
  esac

  # If the date row already exists, replace it; else append.
  if grep -qE "^\| $DATE \|" "$BRAIN_PAGE"; then
    sed -i "s|^\| $DATE \| .* \|\$|\| $DATE \| $VALUE \||" "$BRAIN_PAGE"
  else
    # Insert before the "## Stats" header to keep table grouped
    awk -v row="| $DATE | $VALUE |" '
      /^## Stats/ && !inserted { print row; inserted=1 }
      { print }
    ' "$BRAIN_PAGE" > "$BRAIN_PAGE.tmp"
    mv "$BRAIN_PAGE.tmp" "$BRAIN_PAGE"
  fi

  # Sort the log table by date. Robust against blank lines INSIDE the
  # table region (a known-bad state from earlier versions of this
  # script). The previous regex required (?:\|.*\n)* which broke on
  # the first blank line — leaving subsequent rows unsorted at the bottom.
  # New approach: capture everything between the header dashes and
  # `## Stats`, drop blank/non-pipe lines, sort, write back.
  python3 - "$BRAIN_PAGE" <<'PYEOF'
import sys, re
path = sys.argv[1]
src = open(path).read()
m = re.search(
    r"(\| Date \| Read \|\n\|-+\|-+\|\n)([\s\S]*?)(\n*## Stats)",
    src,
)
if m:
    raw = m.group(2)
    # Keep only well-formed `| YYYY-MM-DD | yes/no |` rows; drop blanks
    # and any other accidental content. Sort lexically (= chronological
    # for ISO dates).
    rows = sorted(
        line.rstrip()
        for line in raw.splitlines()
        if re.match(r"^\| \d{4}-\d{2}-\d{2} \| (yes|no) \|$", line.rstrip())
    )
    new = m.group(1) + "\n".join(rows) + ("\n\n" if rows else "\n") + "## Stats"
    src = src[:m.start()] + new + src[m.end():]
    open(path, "w").write(src)
PYEOF

  # Bump frontmatter updated date
  sed -i "s|^updated: .*|updated: $(now_ist '%Y-%m-%d')|" "$BRAIN_PAGE"

  recompute_stats
}

recompute_stats() {
  ensure_page
  python3 - "$BRAIN_PAGE" "$START_DATE" <<'PYEOF'
import sys, re, datetime
path, start_str = sys.argv[1], sys.argv[2]
src = open(path).read()
log_rows = re.findall(r"\| (\d{4}-\d{2}-\d{2}) \| (yes|no) \|", src)
entries = {d: v for d, v in log_rows}

today = datetime.date.today()
start = datetime.date.fromisoformat(start_str)
yesterday = today - datetime.timedelta(days=1)

# Stats math v2 (revised 2026-05-10):
#
# Each window is a CALENDAR-DAY range. Denominator = number of days in
# the window. Numerator = count of days with `yes` in the entries dict.
# Missing days in the MIDDLE of a window count as 0 yes (a miss).
#
# Upper bound = last logged entry on or before yesterday. NOT just
# `yesterday`. This excludes trailing unlogged days from the denominator
# at ping time. Concretely: at tomorrow's 10am ping (today=N+1) about
# yesterday=N, brain has entries through N-1 (N hasn't been answered
# yet). Stats reflect data through N-1, NOT yesterday.
#
# Why: earlier versions used `yesterday` as the upper bound, which
# pulled the unlogged-yet day into the denominator and counted it as
# a miss. That was wrong — the user hasn't been ASKED yet about that
# day. Stats should reflect only days the user has had a chance to
# log.
#
# After the user replies to the ping, the new entry's date becomes
# the new upper bound on the next recompute, sliding all three windows
# forward by exactly the days the user has filled in.

def window_stats(start_d: datetime.date, end_d: datetime.date):
    """Inclusive window [start_d .. end_d]. Returns (yes, total, pct)."""
    if end_d < start_d:
        return (0, 0, 0)
    yes = 0
    total = 0
    cur = start_d
    while cur <= end_d:
        total += 1
        if entries.get(cur.isoformat()) == "yes":
            yes += 1
        cur += datetime.timedelta(days=1)
    pct = round(100 * yes / total) if total else 0
    return (yes, total, pct)

# Upper bound = max entry date <= yesterday. None if no entries yet.
candidate_dates = [
    datetime.date.fromisoformat(d)
    for d in entries
    if datetime.date.fromisoformat(d) <= yesterday
]
upper_bound = max(candidate_dates) if candidate_dates else None

if upper_bound is None:
    # No entries yet — emit zeros gracefully.
    l7 = mo = ov = (0, 0, 0)
else:
    # Last 7 days: 7-day window ending at upper_bound. Clamp the start
    # to the habit's start_date so pre-habit days don't count.
    last7_start = max(upper_bound - datetime.timedelta(days=6), start)
    l7 = window_stats(last7_start, upper_bound)

    # This month: 1st of THIS month (the month `today` is in) through
    # upper_bound. If upper_bound is in a previous month (rare, only
    # right after a month rolls over with no entries yet), window is
    # empty → (0,0,0).
    month_first = today.replace(day=1)
    if upper_bound >= month_first:
        mo = window_stats(month_first, upper_bound)
    else:
        mo = (0, 0, 0)

    # Overall: start_date through upper_bound.
    ov = window_stats(start, upper_bound)

stats_block = f"""## Stats

- **Started:** {start_str}
- **Last 7 days:** {l7[0]}/{l7[1]} — {l7[2]}%
- **This month ({today.strftime('%b %Y')}):** {mo[0]}/{mo[1]} — {mo[2]}%
- **Overall:** {ov[0]}/{ov[1]} — {ov[2]}%
- **Last updated:** {today.isoformat()}
"""

src = re.sub(r"## Stats.*", stats_block, src, count=1, flags=re.DOTALL)
open(path, "w").write(src)
PYEOF
}

format_ping() {
  ensure_page
  recompute_stats

  # First arg (optional) is a target date YYYY-MM-DD; defaults to yesterday IST.
  local TARGET_DATE="${1:-$(yesterday_ist '%Y-%m-%d')}"
  local TARGET_LABEL
  TARGET_LABEL=$(TZ=Asia/Kolkata date -d "$TARGET_DATE" '+%a %d %b' 2>/dev/null || echo "$TARGET_DATE")

  # Pull the current stats lines (allow grep miss → empty, never fail).
  local L7 MO_LABEL MO OV
  L7=$(grep -oP 'Last 7 days:\*\* \K[^\n]+' "$BRAIN_PAGE" | head -1 || true)
  MO_LABEL=$(grep -oP '\*\*This month \(\K[^)]+' "$BRAIN_PAGE" | head -1 || true)
  MO=$(grep -oP "This month \([^)]+\):\*\* \K[^\n]+" "$BRAIN_PAGE" | head -1 || true)
  OV=$(grep -oP 'Overall:\*\* \K[^\n]+' "$BRAIN_PAGE" | head -1 || true)

  # Loud-fail if recompute_stats produced an unparseable page. Better to
  # surface this early than ship a Telegram message with empty stats.
  if [ -z "$L7" ] || [ -z "$OV" ]; then
    echo "[reading-checkin] FAIL: stats parse failed (L7='$L7' OV='$OV')" >&2
    return 1
  fi

  # If TARGET_DATE is yesterday, phrase it as "yesterday"; else explicit.
  local PROMPT_DATE
  if [ "$TARGET_DATE" = "$(yesterday_ist '%Y-%m-%d')" ]; then
    PROMPT_DATE="yesterday ($TARGET_LABEL)"
  else
    PROMPT_DATE="on $TARGET_LABEL ($TARGET_DATE)"
  fi

  # Friend-voice rewrite (2026-05-24, see identity/SOUL.md):
  # the old version led with a "📚 Reading check-in" header and ended
  # with a long "Please reply yes / no..." menu — both engineer-voice
  # tells. The new shape is a single warm line that surfaces the most
  # interesting stat as a nudge, plus the prompt itself.
  cat <<MSG
📚 did you read $PROMPT_DATE? (you're at $L7 this week, $MO_LABEL is $MO so far)
MSG
}

commit_brain() {
  cd "$HOME/.brain"
  if [ -n "$(git status --porcelain habits/book-reading.md 2>/dev/null)" ]; then
    git add habits/book-reading.md
    git -c user.email='arete-bot@${PLATFORM_ROOT_DOMAIN}' -c user.name='Arete' \
      commit -m "habit: book-reading update $(now_ist '%Y-%m-%d %H:%M')" >/dev/null 2>&1 || true
  fi
  # Push every habit commit immediately so GitHub reflects the change
  # in real time (single-writer system, conflicts are vanishingly rare).
  # Network failure logs a warn but never blocks the user-facing reply —
  # the next push (or the daily collector at 7am) will catch up.
  if git log origin/main..HEAD --oneline 2>/dev/null | grep -q .; then
    if git push origin main 2>/dev/null; then
      echo "[reading-checkin] pushed brain to origin"
    else
      echo "[reading-checkin] WARN: git push failed; commits will retry on next call or via daily collector" >&2
    fi
  fi
}

case "${1:-}" in
  --smoke)
    # Cold-boot callability check — no state writes, no network.
    echo '{"ok":true,"skill":"reading-checkin","user_message":"reading-checkin smoke ok"}'
    exit 0
    ;;
  ping)
    # ping              → ask about yesterday
    # ping --for <date> → ask about a specific date (backfill / catch-up)
    TARGET_DATE=""
    if [ "${2:-}" = "--for" ] && [ -n "${3:-}" ]; then
      TARGET_DATE="$3"
      # Sanity-check date format
      if ! TZ=Asia/Kolkata date -d "$TARGET_DATE" >/dev/null 2>&1; then
        echo "[reading-checkin] FAIL: --for received invalid date '$TARGET_DATE'" >&2
        exit 1
      fi
    fi
    MSG=$(format_ping "$TARGET_DATE")
    ${STATE_HOME}/bin/send-telegram.sh "$MSG"
    echo "[reading-checkin] OK: ping sent for ${TARGET_DATE:-yesterday}"
    commit_brain
    ;;
  log)
    DATE="${3:-$(yesterday_ist '%Y-%m-%d')}"
    log_entry "$DATE" "$2"
    commit_brain
    echo "logged $DATE = $2"
    ;;
  stats)
    recompute_stats
    grep -A 10 '^## Stats' "$BRAIN_PAGE"
    ;;
  verify)
    # Data integrity check: list every date from START_DATE to yesterday
    # and flag any that are missing from the brain log table. The brain
    # is the single source of truth — if a date is missing here, stats
    # will be wrong even if the user logged it elsewhere.
    ensure_page
    python3 - "$BRAIN_PAGE" "$START_DATE" <<'PYEOF'
import sys, re, datetime
path, start_str = sys.argv[1], sys.argv[2]
src = open(path).read()
log_rows = re.findall(r"\| (\d{4}-\d{2}-\d{2}) \| (yes|no) \|", src)
entries = dict(log_rows)
today = datetime.date.today()
start = datetime.date.fromisoformat(start_str)
yesterday = today - datetime.timedelta(days=1)

missing = []
present = []
d = start
while d <= yesterday:
    iso = d.isoformat()
    if iso in entries:
        present.append((iso, entries[iso]))
    else:
        missing.append(iso)
    d += datetime.timedelta(days=1)

print(f"Reading-checkin verify (range: {start_str} → {yesterday.isoformat()})")
print(f"  present: {len(present)} days")
print(f"  missing: {len(missing)} days")
if missing:
    print("\nMISSING DATES (run `reading-checkin.sh log yes|no <date>` to backfill):")
    for m in missing:
        print(f"  - {m}")
    sys.exit(2)
else:
    print("\n✅ No gaps. All days from start to yesterday are logged.")
PYEOF
    ;;
  *)
    echo "Usage: reading-checkin.sh {ping|log <yes|no> [date]|stats}" >&2
    exit 1
    ;;
esac
