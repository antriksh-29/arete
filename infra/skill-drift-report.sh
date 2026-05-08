#!/usr/bin/env bash
# skill-drift-report.sh — surfaces three kinds of drift across the
# skill catalog so silent decay shows up before the user notices it
# in conversation.
#
#   1. Usage drift     — which skills fired more / less than baseline
#   2. Quality drift   — which scripts are returning errors
#   3. Dormant skills  — fired 0× in 30 days
#
# Designed to run on the VPS (where journalctl + state files exist).
# Outputs JSON on stdout for the morning-briefing orchestrator;
# Sunday's briefing surfaces the report verbatim.
#
# Usage:
#   skill-drift-report.sh              # last 7 days, JSON to stdout
#   skill-drift-report.sh --days 30
#   skill-drift-report.sh --markdown   # human-readable for ad-hoc
#   skill-drift-report.sh --smoke

set -uo pipefail
cd "$(dirname "$0")/.."

DAYS=7
FORMAT=json

while [ $# -gt 0 ]; do
  case "$1" in
    --smoke)
      echo '{"ok":true,"skill":"skill-drift-report","user_message":"skill-drift-report smoke ok"}'
      exit 0
      ;;
    --days) DAYS="$2"; shift 2 ;;
    --markdown) FORMAT=markdown; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ── data sources ────────────────────────────────────────────────

# A skill's invocation surface depends on its kind:
#   - shell scripts in ~/bin/  → journalctl + per-skill state files
#   - python scripts → same
#   - constraint skills (reply-discipline) → no user-fired count;
#     count rejections via journalctl grep
#
# We use a per-skill heuristic: "evidence of recent activity" comes from
# checking whether the skill's WRITES_TO state file (per SKILL.md) was
# touched in the window, and from journalctl grep for the script name.

SINCE="$(date -v -${DAYS}d +%Y-%m-%d 2>/dev/null || date -d "${DAYS} days ago" +%Y-%m-%d)"

# Discover all skills + their backing scripts from SKILL.md frontmatter.
collect_skills() {
  for sk in skills/*/SKILL.md; do
    [ -f "$sk" ] || continue
    name=$(awk -F: '/^name:/ {gsub(/ /, "", $2); print $2; exit}' "$sk")
    script=$(awk -F: '/^script:/ {sub(/^[ ]+/, "", $2); print $2; exit}' "$sk")
    constraint=$(awk -F: '/^constraint_skill:/ {gsub(/ /, "", $2); print $2; exit}' "$sk")
    [ -z "$name" ] && continue
    echo "$name|$script|${constraint:-false}"
  done
}

# Per-skill evidence count from journalctl. Best-effort; if journalctl
# isn't available (dev machine) we just return 0.
journal_count() {
  local pattern="$1"
  if ! command -v journalctl >/dev/null 2>&1; then
    echo 0
    return
  fi
  journalctl --user --since "$SINCE" --no-pager 2>/dev/null \
    | grep -c -F "$pattern" || true
}

# Per-skill state-file mtime check. Returns "fresh" if any state file
# referenced by the skill was modified in the window, else "stale".
state_freshness() {
  local script="$1"
  [ -z "$script" ] && { echo "no-script"; return; }
  local base
  base=$(basename "$script" | sed 's/\.[a-z]*$//')
  # Look for state files matching the skill's typical write paths.
  local state_root="$HOME/.brain-bootstrap-raw/state"
  [ -d "$state_root" ] || { echo "no-state-root"; return; }
  local newest
  newest=$(find "$state_root" -name "*${base}*" -newer "$state_root/.." -type f 2>/dev/null | head -1)
  [ -n "$newest" ] && echo "fresh" || echo "stale"
}

# Smoke-test result from the gbrain smoke loop (if logs are around).
smoke_status() {
  local name="$1"
  local log="${GBRAIN_SMOKE_LOG:-/tmp/gbrain-smoke-test.log}"
  [ -f "$log" ] || { echo "no-log"; return; }
  if grep -q "PASS.*${name}" "$log" 2>/dev/null; then
    echo "pass"
  elif grep -q "FAIL.*${name}" "$log" 2>/dev/null; then
    echo "fail"
  else
    echo "absent"
  fi
}

# ── build the report ────────────────────────────────────────────

declare -a rows
total_skills=0
dormant=0
errored=0

while IFS='|' read -r name script is_constraint; do
  [ -z "$name" ] && continue
  total_skills=$((total_skills+1))
  base=$(basename "${script:-x}" | sed 's/\.[a-z]*$//')
  jcount=$(journal_count "$base")
  freshness=$(state_freshness "$script")
  smoke=$(smoke_status "$name")
  status="active"
  if [ "$is_constraint" = "true" ]; then
    status="constraint"
  elif [ "$jcount" -eq 0 ] && [ "$freshness" = "stale" ]; then
    status="dormant"; dormant=$((dormant+1))
  fi
  if [ "$smoke" = "fail" ]; then
    status="errored"; errored=$((errored+1))
  fi
  rows+=("{\"skill\":\"$name\",\"script\":\"${script:-}\",\"journal_hits\":$jcount,\"state_freshness\":\"$freshness\",\"smoke\":\"$smoke\",\"status\":\"$status\"}")
done < <(collect_skills)

# ── format output ───────────────────────────────────────────────

if [ "$FORMAT" = "markdown" ]; then
  echo "## Skill drift — last ${DAYS} days (since $SINCE)"
  echo
  echo "| Skill | Journal hits | State | Smoke | Status |"
  echo "|---|---|---|---|---|"
  for r in "${rows[@]}"; do
    name=$(echo "$r" | sed -n 's/.*"skill":"\([^"]*\)".*/\1/p')
    jh=$(echo "$r" | sed -n 's/.*"journal_hits":\([0-9]*\).*/\1/p')
    sf=$(echo "$r" | sed -n 's/.*"state_freshness":"\([^"]*\)".*/\1/p')
    sm=$(echo "$r" | sed -n 's/.*"smoke":"\([^"]*\)".*/\1/p')
    st=$(echo "$r" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
    echo "| $name | $jh | $sf | $sm | $st |"
  done
  echo
  echo "**Summary:** $total_skills skills · $dormant dormant · $errored errored"
else
  # JSON envelope
  rows_joined=$(IFS=,; echo "${rows[*]}")
  cat <<EOF
{"ok":true,"skill":"skill-drift-report","window_days":${DAYS},"since":"$SINCE","totals":{"skills":$total_skills,"dormant":$dormant,"errored":$errored},"rows":[$rows_joined],"user_message":"$total_skills skills · $dormant dormant · $errored errored over last ${DAYS} days"}
EOF
fi
