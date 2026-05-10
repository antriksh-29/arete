#!/bin/bash
# Activity tracker — Strava nightly sync + manual-log + brain rebuild.
#
# Single source of truth: ~/.brain/habits/activity-tracker.md (rebuilt
# on every sync from activities.tsv + manual.tsv). fitness-snapshot.py
# and any other reader pull from this brain page only.
#
# Token refresh: Strava access tokens expire in ~6h. Before any API call
# we check expires_at and refresh via the OAuth endpoint if needed.
# Without this the cron silently fails after token expiry (the May 6-8
# bug — three days of empty data).
#
# Sync safety: activities.tsv + seen_ids are NOT truncated until we
# have a successful first API page. On failure (network, expired
# refresh token, etc.) the previous data stays intact.
set -euo pipefail
STATE_DIR="$HOME/.brain-bootstrap-raw/state/activity-tracker"
TARGET=200
YEAR=$(TZ=Asia/Kolkata date +%Y)
mkdir -p "$STATE_DIR"
TOKEN_FILE="$STATE_DIR/strava_tokens.json"
SEEN_FILE="$STATE_DIR/seen_ids"
ACT_FILE="$STATE_DIR/activities.tsv"
MANUAL_FILE="$STATE_DIR/manual.tsv"
BRAIN_DIR="$HOME/.brain"
BRAIN_PAGE="$BRAIN_DIR/habits/activity-tracker.md"
BRAIN_SLUG="habits/activity-tracker"
GBRAIN="/home/botuser/.bun/bin/gbrain"
STRAVA_CLIENT_ID="236081"
touch "$MANUAL_FILE"

# Refresh token if expired or expiring within 5 min.
ensure_token() {
  local now expires_at refresh
  now=$(date +%s)
  expires_at=$(jq -r '.expires_at // 0' "$TOKEN_FILE")
  if [ "$expires_at" -gt "$((now + 300))" ]; then
    return 0  # still valid
  fi
  refresh=$(jq -r '.refresh_token' "$TOKEN_FILE")
  if [ -z "${STRAVA_CLIENT_SECRET:-}" ]; then
    echo "ERROR: STRAVA_CLIENT_SECRET not set; cannot refresh token" >&2
    return 1
  fi
  echo "[$(date -Iseconds)] strava token expired (expires_at=$expires_at, now=$now) — refreshing" >&2
  local resp
  resp=$(curl -sS -X POST "https://www.strava.com/api/v3/oauth/token" \
    -d "client_id=$STRAVA_CLIENT_ID" \
    -d "client_secret=$STRAVA_CLIENT_SECRET" \
    -d "grant_type=refresh_token" \
    -d "refresh_token=$refresh")
  local new_access
  new_access=$(echo "$resp" | jq -r '.access_token // empty')
  if [ -z "$new_access" ]; then
    echo "ERROR: refresh failed: $resp" >&2
    return 1
  fi
  echo "$resp" > "$TOKEN_FILE"
  echo "[$(date -Iseconds)] strava token refreshed (new expires_at=$(jq -r .expires_at "$TOKEN_FILE"))" >&2
}

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
  ensure_token
  local after end raw page count tmp_act tmp_seen
  after=$(date -d "$YEAR-01-01 00:00:00" +%s)
  end=$(date -d "$((YEAR+1))-01-01 00:00:00" +%s)
  page=1
  # Build into temp files; only swap into place on success so a failed
  # sync never empties the canonical files.
  tmp_act=$(mktemp); tmp_seen=$(mktemp)
  while :; do
    raw=$(api_get "/athlete/activities?after=$after&page=$page&per_page=200")
    # If the API returned an error object, jq 'length' would be 2 (key
    # count). Detect explicitly.
    if echo "$raw" | jq -e 'type == "object" and has("errors")' >/dev/null 2>&1; then
      echo "ERROR: strava API error: $raw" >&2
      rm -f "$tmp_act" "$tmp_seen"
      return 1
    fi
    count=$(echo "$raw" | jq 'length')
    [[ "$count" -eq 0 ]] && break
    echo "$raw" | jq -r '.[] | select((.start_date | fromdateiso8601) >= '$after' and (.start_date | fromdateiso8601) < '$end') | [.id,.name,.type,(.sport_type // ""),.start_date] | @tsv' |
    while IFS=$'\t' read -r id name type sport start; do
      grep -qx "$id" "$tmp_seen" 2>/dev/null && continue
      cat=$(classify "$name" "$type" "$sport")
      echo "$id" >> "$tmp_seen"
      printf '%s\t%s\t%s\t%s\n' "$id" "$start" "$cat" "$name" >> "$tmp_act"
    done
    page=$((page+1))
  done
  mv "$tmp_act" "$ACT_FILE"
  mv "$tmp_seen" "$SEEN_FILE"
  rebuild_brain
  echo "[$(date -Iseconds)] sync complete: $(wc -l < "$ACT_FILE") strava entries, $(wc -l < "$MANUAL_FILE") manual entries"
}

# manual-log <category> <duration_min> <title> [YYYY-MM-DD]
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
  rebuild_brain
  echo "✓ Logged: $category — $title (${duration}min) on ${start%T*}"
}

# Compute counters from activities.tsv + manual.tsv combined.
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

# Count sessions in current ISO week (Mon-Sun) from merged data.
week_count() {
  local merged start_of_week today_dow
  merged=$(cat "$ACT_FILE" "$MANUAL_FILE" 2>/dev/null || true)
  today_dow=$(TZ=Asia/Kolkata date +%u)  # 1=Mon
  start_of_week=$(TZ=Asia/Kolkata date -d "$((today_dow - 1)) days ago" +%Y-%m-%d)
  echo "$merged" | awk -F '\t' -v sow="$start_of_week" '
    {
      # parts[2] is iso start; take YYYY-MM-DD prefix
      d = substr($2, 1, 10)
      if (d >= sow) c++
    } END { print c+0 }'
}

# Recompute the brain page from activities.tsv + manual.tsv. Idempotent.
# This is the single-source-of-truth write path. fitness-snapshot.py
# reads from this page only.
rebuild_brain() {
  mkdir -p "$(dirname "$BRAIN_PAGE")"
  local merged gym running tennis others total pct wk today
  merged=$(cat "$ACT_FILE" "$MANUAL_FILE" 2>/dev/null || true)
  gym=$(echo "$merged" | awk -F '\t' '$3=="gym" {c++} END{print c+0}')
  running=$(echo "$merged" | awk -F '\t' '$3=="running" {c++} END{print c+0}')
  tennis=$(echo "$merged" | awk -F '\t' '$3=="tennis" {c++} END{print c+0}')
  others=$(echo "$merged" | awk -F '\t' '$3=="others" {c++} END{print c+0}')
  total=$((gym+running+tennis+others))
  pct=$(awk -v t="$total" -v target="$TARGET" 'BEGIN{printf "%.1f", (t/target)*100}')
  wk=$(week_count)
  today=$(TZ=Asia/Kolkata date +%Y-%m-%d)

  cat > "$BRAIN_PAGE" <<EOF
---
type: habit
title: Activity Tracker
name: Activity Tracker
created: '2026-01-01T00:00:00.000Z'
updated: '${today}T00:00:00.000Z'
tags: [fitness, strava, tracker]
---

# Activity Tracker

Auto-rebuilt by \`~/bin/activity-tracker.sh\`. Source of truth is
the merge of \`~/.brain-bootstrap-raw/state/activity-tracker/activities.tsv\`
(Strava sync) and \`manual.tsv\` (non-Strava entries). DO NOT
hand-edit this page — changes are overwritten on every sync /
manual-log call.

## Current metrics

- Total sessions this year: ${total}
- Target: ${TARGET}
- % achieved: ${pct}%
- This week (Mon-Sun): ${wk}

## Canonical breakdown for ${YEAR}

- Tennis: ${tennis}
- Gym: ${gym}
- Running: ${running}
- Others: ${others}

## Category rules

- **Gym:** traditional gym + functional strength training + weight training + therapy
- **Running:** all running activities
- **Tennis:** all tennis activities
- **Others:** cricket, pickleball, ride, and similar non-core sports

## Integration

- **Strava app client ID:** ${STRAVA_CLIENT_ID}
- **Last sync:** ${today}
- **Auth status:** auto-refreshed via refresh_token
EOF

  if [ -x "$GBRAIN" ]; then
    # Don't swallow errors — if gbrain put fails, the brief reads stale
    # DB data (May 8 incident). Surface failures to the journal.
    if ! "$GBRAIN" put "$BRAIN_SLUG" < "$BRAIN_PAGE" >/tmp/gbrain-put.log 2>&1; then
      echo "[$(date -Iseconds)] gbrain put failed: $(cat /tmp/gbrain-put.log)" >&2
    fi
  fi

  if [ -d "$BRAIN_DIR/.git" ]; then
    (
      cd "$BRAIN_DIR"
      git add habits/activity-tracker.md 2>/dev/null
      if ! git diff --cached --quiet 2>/dev/null; then
        git -c user.email='alfred@bot' -c user.name='Alfred' \
          commit -m "activity-tracker: rebuild $today" >/dev/null 2>&1 || true
      fi
      # Push immediately so GitHub reflects state in real time.
      if git log origin/main..HEAD --oneline 2>/dev/null | grep -q .; then
        git push origin main >/dev/null 2>&1 || \
          echo "[activity-tracker] WARN: git push failed; will retry next run" >&2
      fi
    )
  fi
}

# Verify integrity: report data freshness + token expiry.
verify() {
  echo "=== Activity tracker verify ==="
  echo "activities.tsv: $(wc -l < "$ACT_FILE") entries (mtime: $(stat -c %y "$ACT_FILE" 2>/dev/null))"
  echo "manual.tsv:     $(wc -l < "$MANUAL_FILE") entries"
  echo "brain page:     $([ -f "$BRAIN_PAGE" ] && echo present || echo MISSING)"
  local now expires_at
  now=$(date +%s)
  expires_at=$(jq -r '.expires_at // 0' "$TOKEN_FILE")
  if [ "$expires_at" -lt "$now" ]; then
    echo "strava token:   EXPIRED ($((now - expires_at))s ago) — next sync will refresh"
  else
    echo "strava token:   valid for $((expires_at - now))s"
  fi
}

case "${1:-help}" in
  --smoke)
    echo '{"ok":true,"skill":"activity-tracker","user_message":"activity-tracker smoke ok"}'
    exit 0
    ;;
  sync) sync ;;
  stats) stats ;;
  manual-log) shift; manual_log "$@" ;;
  rebuild-brain) rebuild_brain ;;
  verify) verify ;;
  *) echo 'Usage: activity-tracker.sh <sync|stats|manual-log <cat> <min> <title> [date]|rebuild-brain|verify>' ;;
esac
