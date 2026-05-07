#!/bin/bash
# Activity tracker — Strava nightly sync + manual-log for non-Strava sessions.
#
# Strava activities live in $ACT_FILE (rebuilt fresh on every sync).
# Manual entries (yoga, squash, padel, etc.) live in $MANUAL_FILE which
# persists across syncs. stats merges both files.
set -euo pipefail
STATE_DIR="$HOME/.brain-bootstrap-raw/state/activity-tracker"
TARGET=200
YEAR=$(TZ=Asia/Kolkata date +%Y)
mkdir -p "$STATE_DIR"
TOKEN_FILE="$STATE_DIR/strava_tokens.json"
SEEN_FILE="$STATE_DIR/seen_ids"
ACT_FILE="$STATE_DIR/activities.tsv"
MANUAL_FILE="$STATE_DIR/manual.tsv"
touch "$MANUAL_FILE"

load_token() { jq -r '.access_token' "$TOKEN_FILE"; }

api_get() {
  local path="$1"
  local token
  token=$(load_token)
  curl -sS -H "Authorization: Bearer $token" "https://www.strava.com/api/v3$path"
}

classify() {
  local name="$1" type="$2" sport="$3"
  shopt -s nocasematch
  if [[ "$sport" =~ run ]] || [[ "$type" =~ Run ]] || [[ "$name" =~ run ]]; then echo running
  elif [[ "$sport" =~ tennis ]] || [[ "$name" =~ tennis ]]; then echo tennis
  elif [[ "$sport" =~ workout|weight|strength ]] || [[ "$name" =~ gym|lift|strength ]]; then echo gym
  else echo others
  fi
}

sync() {
  : > "$SEEN_FILE"
  : > "$ACT_FILE"
  local after end raw page count
  after=$(date -d "$YEAR-01-01 00:00:00" +%s)
  end=$(date -d "$((YEAR+1))-01-01 00:00:00" +%s)
  page=1
  while :; do
    raw=$(api_get "/athlete/activities?after=$after&page=$page&per_page=200")
    count=$(echo "$raw" | jq 'length')
    [[ "$count" -eq 0 ]] && break
    echo "$raw" | jq -r '.[] | select((.start_date | fromdateiso8601) >= '$after' and (.start_date | fromdateiso8601) < '$end') | [.id,.name,.type,(.sport_type // ""),.start_date] | @tsv' |
    while IFS=$'\t' read -r id name type sport start; do
      grep -qx "$id" "$SEEN_FILE" && continue
      cat=$(classify "$name" "$type" "$sport")
      echo "$id" >> "$SEEN_FILE"
      printf '%s\t%s\t%s\t%s\n' "$id" "$start" "$cat" "$name" >> "$ACT_FILE"
    done
    page=$((page+1))
  done
}

# manual-log <category> <duration_min> <title> [YYYY-MM-DD]
# Persists to $MANUAL_FILE so nightly Strava sync can't wipe it.
manual_log() {
  local category="${1:?category (gym|running|tennis|others)}"
  local duration="${2:?duration in minutes}"
  local title="${3:?title}"
  local date_arg="${4:-}"
  local start
  if [ -n "$date_arg" ]; then
    start="${date_arg}T12:00:00+05:30"
  else
    start=$(TZ=Asia/Kolkata date -Iseconds)
  fi
  case "$category" in
    gym|running|tennis|others) ;;
    *) echo "Invalid category: $category (use gym|running|tennis|others)" >&2; return 1 ;;
  esac
  local id="manual-$(date +%s)-$RANDOM"
  printf '%s\t%s\t%s\t%s (%smin manual)\n' "$id" "$start" "$category" "$title" "$duration" >> "$MANUAL_FILE"
  echo "✓ Logged: $category — $title (${duration}min) on ${start%T*}"
}

stats() {
  local merged
  merged=$(cat "$ACT_FILE" "$MANUAL_FILE" 2>/dev/null || true)
  local gym running tennis others total pct
  gym=$(echo "$merged" | awk -F '\t' '$3=="gym" {c++} END{print c+0}')
  running=$(echo "$merged" | awk -F '\t' '$3=="running" {c++} END{print c+0}')
  tennis=$(echo "$merged" | awk -F '\t' '$3=="tennis" {c++} END{print c+0}')
  others=$(echo "$merged" | awk -F '\t' '$3=="others" {c++} END{print c+0}')
  total=$((gym+running+tennis+others))
  pct=$(awk -v t="$total" -v target="$TARGET" 'BEGIN{printf "%.1f", (t/target)*100}')
  printf 'Gym: %s\nRunning: %s\nTennis: %s\nOthers: %s\nTotal: %s\n%% achieved: %s%%\n' "$gym" "$running" "$tennis" "$others" "$total" "$pct"
}

case "${1:-help}" in
  sync) sync ;;
  stats) stats ;;
  manual-log) shift; manual_log "$@" ;;
  *) echo 'Usage: activity-tracker.sh <sync|stats|manual-log <cat> <min> <title> [date]>' ;;
esac
