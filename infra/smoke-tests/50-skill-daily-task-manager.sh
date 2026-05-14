#!/usr/bin/env bash
# Smoke test for the daily-task-manager skill.
# 1. --smoke check (callable, returns ok=true)
# 2. B1 regression guards on `review` — the bot must NOT be able to
#    silently apply 0 status updates without the script erroring.
#    See docs/PLAN-daily-task-manager-v2.md and the May 2026 outage
#    where a missing-stdin invocation produced "✓ Applied 0 status
#    updates" and the user lost a day of evening-review data.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"

SCRIPT="$HOME/bin/daily-tasks.sh"

# Step 1: cold-boot --smoke
run_smoke "daily-task-manager" "bash" "$SCRIPT" || exit 1

# Step 2: regression guards. Use an isolated HOME so we don't pollute
# real state. Seed a fake state file with 3 tasks, then exercise the
# three guarded paths.
TMP="$(mktemp -d -t dtm-test-XXXX)"
trap 'rm -rf "$TMP"' EXIT

# Phase 2: seed the v2 task-centric layout so `review` has tasks to
# operate on. Three tasks → three task files + a daily pointer file.
V2="$TMP/.brain-bootstrap-raw/state/daily-tasks/v2"
mkdir -p "$V2/tasks" "$V2/daily"
TODAY=$(TZ=Asia/Kolkata date +%Y-%m-%d)
NOW="$(date -u +%Y-%m-%dT%H:%M:%S+05:30)"

IDS=()
for TITLE in alpha beta gamma; do
  ID=$(python3 -c "import uuid;print(uuid.uuid4())")
  IDS+=("$ID")
  NORM=$(echo "$TITLE" | tr '[:upper:]' '[:lower:]')
  cat > "$V2/tasks/$ID.json" <<EOF
{
  "id": "$ID",
  "title": "$TITLE",
  "title_norm": "$NORM",
  "created_date": "$TODAY",
  "created_at": "$NOW",
  "events": [{"date":"$TODAY","status":"open","ts":"$NOW"}],
  "current_status": "open",
  "closed_at": null
}
EOF
done

# Comma-join IDs as JSON array
IDS_JSON=$(printf '"%s",' "${IDS[@]}" | sed 's/,$//')
cat > "$V2/daily/$TODAY.json" <<EOF
{"date":"$TODAY","task_ids":[$IDS_JSON]}
EOF

fail() { echo "✗ daily-task-manager guards FAILED: $1"; exit 1; }

# Guard 1: no input at all (no --reply, no stdin) → exit 2
HOME="$TMP" bash "$SCRIPT" review < /dev/null >/dev/null 2>&1
rc=$?
[ "$rc" -eq 2 ] || fail "no-input did not exit 2 (got $rc)"

# Guard 2: --reply with empty string → exit 2
HOME="$TMP" bash "$SCRIPT" review --reply "" < /dev/null >/dev/null 2>&1
rc=$?
[ "$rc" -eq 2 ] || fail "empty --reply did not exit 2 (got $rc)"

# Guard 3: --reply with non-numbered text → exit 3
HOME="$TMP" bash "$SCRIPT" review --reply "no statuses here" < /dev/null >/dev/null 2>&1
rc=$?
[ "$rc" -eq 3 ] || fail "non-numbered --reply did not exit 3 (got $rc)"

# Happy path: valid --reply → exit 0 + statuses populated
HOME="$TMP" bash "$SCRIPT" review --reply "1. done
2. tomorrow
3. later" < /dev/null >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] || fail "valid --reply did not exit 0 (got $rc)"

# Verify state was actually mutated (3 task files now have an event
# for today with a non-open status: yes / tomorrow / later).
mutated=0
for ID in "${IDS[@]}"; do
  last_status=$(jq -r '.current_status' "$V2/tasks/$ID.json" 2>/dev/null)
  case "$last_status" in
    yes|tomorrow|later) mutated=$((mutated + 1)) ;;
  esac
done
[ "$mutated" -eq 3 ] || fail "expected 3 tasks with non-open status, got $mutated"

echo "✓ daily-task-manager review guards ok (exit 2 / 2 / 3 / 0; 3 statuses applied)"
