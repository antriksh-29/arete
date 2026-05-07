#!/bin/bash
# Daily tasks — single source of truth: state JSON per date.
#
# State file:  ~/.brain-bootstrap-raw/state/daily-tasks/<YYYY-MM-DD>.json
# Brain view:  habits/daily-tasks  (auto-rebuilt on every mutation)
#
# Subcommands:
#   set <task1> <task2> ...   REPLACE today's list. Accepts stdin (multi-line)
#                              if no args given. Use when user says "my tasks
#                              today are: ...".
#   add <task1> ...           APPEND to today's list. Stdin too. Use when user
#                              says "add X to my list" or shares additional items.
#   remove <index|text>       Delete a task. Index is 1-based; text matches
#                              substring (case-insensitive).
#   mark <index> <status>     Set status: yes | tomorrow | later | delete.
#   show [date]               Print today's (or specified date's) tasks in
#                              Telegram-friendly format. date can be:
#                              YYYY-MM-DD, today, yesterday, or N days back as -1, -2, etc.
#   carryover                 Yesterday's pushed-to-today + indefinite items.
#                              Used by morning briefing — do NOT change shape.
#   status                    Raw JSON for debugging.
#
# After any mutation (set/add/remove/mark) the script rebuilds the
# habits/daily-tasks brain page so the agent can find current state via
# the brain too. Brain repo gets git-committed so changes land in backups.

set -euo pipefail

# gbrain has #!/usr/bin/env bun shebang — needs bun on PATH whenever this
# script invokes gbrain (rebuild_brain). Cron + openclaw both lose PATH.
export PATH="/home/botuser/.bun/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STATE_DIR="$HOME/.brain-bootstrap-raw/state/daily-tasks"
BRAIN_DIR="$HOME/.brain"
BRAIN_PAGE_PATH="$BRAIN_DIR/habits/daily-tasks.md"
BRAIN_SLUG="habits/daily-tasks"
GBRAIN="/home/botuser/.bun/bin/gbrain"

mkdir -p "$STATE_DIR"

today() { TZ=Asia/Kolkata date +%Y-%m-%d; }
yesterday() { TZ=Asia/Kolkata date -d "1 day ago" +%Y-%m-%d; }

# Resolve a date arg: empty = today, "today" = today, "yesterday" = yesterday,
# YYYY-MM-DD = literal, "-N" = N days ago.
resolve_date() {
  local arg="${1:-today}"
  case "$arg" in
    today|"") today ;;
    yesterday) yesterday ;;
    -[0-9]*) TZ=Asia/Kolkata date -d "${arg#-} days ago" +%Y-%m-%d ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) echo "$arg" ;;
    *) echo "Unrecognized date: $arg (use YYYY-MM-DD, today, yesterday, or -N)" >&2; return 1 ;;
  esac
}

# Read tasks from args; if none, read from stdin (one task per line).
# Echoes JSON array of task strings.
#
# Robustness: if exactly ONE arg is given and it contains commas or
# newlines, split on those. This is for cases where the LLM caller
# accidentally passes a single comma-joined string instead of multiple
# quoted args.
collect_tasks() {
  if [ "$#" -eq 1 ] && [[ "$1" == *,* || "$1" == *$'\n'* ]]; then
    printf '%s' "$1" | tr ',\n' '\n\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;/^$/d' | jq -R . | jq -s .
  elif [ "$#" -gt 0 ]; then
    printf '%s\n' "$@" | sed '/^$/d' | jq -R . | jq -s .
  else
    sed '/^$/d' | jq -R . | jq -s .
  fi
}

# -------------------- mutations --------------------

cmd_set() {
  local d
  d=$(today)
  local f="$STATE_DIR/$d.json"
  local tasks_json
  tasks_json=$(collect_tasks "$@")
  local n
  n=$(echo "$tasks_json" | jq 'length')

  if [ "$n" -eq 0 ]; then
    echo "Refusing to set empty list — use remove or mark delete to clear individual items." >&2
    return 1
  fi

  jq -n \
    --arg date "$d" \
    --argjson tasks "$tasks_json" \
    '{date: $date, tasks: $tasks, statuses: {}}' \
    > "$f"

  rebuild_brain
  echo "✓ Stored $n tasks for $d (replaced any prior list)"
}

cmd_add() {
  local d
  d=$(today)
  local f="$STATE_DIR/$d.json"
  local new_json
  new_json=$(collect_tasks "$@")
  local new_n
  new_n=$(echo "$new_json" | jq 'length')

  if [ "$new_n" -eq 0 ]; then
    echo "Nothing to add (empty input)." >&2
    return 1
  fi

  if [ ! -f "$f" ]; then
    jq -n \
      --arg date "$d" \
      --argjson tasks "$new_json" \
      '{date: $date, tasks: $tasks, statuses: {}}' \
      > "$f"
    rebuild_brain
    echo "✓ Created today's list with $new_n tasks for $d"
    return
  fi

  # Append, dedup against existing
  local merged
  merged=$(jq --argjson new "$new_json" \
    '.tasks = (.tasks + $new | unique_by(ascii_downcase | gsub(" "; "")))' \
    "$f")
  echo "$merged" > "$f"
  local total
  total=$(echo "$merged" | jq '.tasks | length')
  rebuild_brain
  echo "✓ Added $new_n; today now has $total tasks"
}

cmd_remove() {
  local arg="${1:?Usage: remove <index|text>}"
  local d
  d=$(today)
  local f="$STATE_DIR/$d.json"

  if [ ! -f "$f" ]; then
    echo "No tasks for today ($d)." >&2
    return 1
  fi

  if [[ "$arg" =~ ^[0-9]+$ ]]; then
    # Index-based remove
    local idx=$((arg - 1))
    local task
    task=$(jq -r ".tasks[$idx] // empty" "$f")
    if [ -z "$task" ]; then
      echo "No task at index $arg." >&2
      return 1
    fi
    jq --arg t "$task" \
      '.tasks = (.tasks | map(select(. != $t))) | del(.statuses[$t])' \
      "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    rebuild_brain
    echo "✓ Removed: $task"
  else
    # Text-based: case-insensitive substring match
    local matches
    matches=$(jq -r --arg q "$arg" '.tasks[] | select(ascii_downcase | contains($q | ascii_downcase))' "$f")
    if [ -z "$matches" ]; then
      echo "No task matching: $arg" >&2
      return 1
    fi
    local count
    count=$(echo "$matches" | wc -l | tr -d ' ')
    if [ "$count" -gt 1 ]; then
      echo "Ambiguous — multiple matches for '$arg':" >&2
      echo "$matches" | sed 's/^/  - /' >&2
      return 1
    fi
    jq --arg q "$arg" \
      '.tasks = (.tasks | map(select(ascii_downcase | contains($q | ascii_downcase) | not)))' \
      "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    rebuild_brain
    echo "✓ Removed: $matches"
  fi
}

# Bulk review: parse a multi-line "1. status\n2. status\n..." input from
# stdin OR args and apply each mark to the most recent state file (defaults
# to today, or use --date YYYY-MM-DD). Used for the 22:30 IST review reply.
cmd_review() {
  local target_date
  if [ "${1:-}" = "--date" ] && [ -n "${2:-}" ]; then
    target_date="$2"
    shift 2
  else
    target_date=$(today)
  fi
  local f="$STATE_DIR/$target_date.json"

  if [ ! -f "$f" ]; then
    echo "No tasks for $target_date." >&2
    return 1
  fi

  local input
  if [ "$#" -gt 0 ]; then
    input="$*"
  else
    input=$(cat)
  fi

  local count=0
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # Match patterns like "1. tomorrow" / "2: done" / "3) later" / "4 - skip"
    local idx status
    idx=$(echo "$line" | grep -oE '^[[:space:]]*[0-9]+' | grep -oE '[0-9]+')
    [ -z "$idx" ] && continue
    status=$(echo "$line" | sed -E 's/^[[:space:]]*[0-9]+[.):\-[:space:]]*//' | tr '[:upper:]' '[:lower:]' | xargs)
    case "$status" in
      yes|y|done|completed|complete) status="yes" ;;
      tomorrow|push*tomorrow|defer|push) status="tomorrow" ;;
      later|push*later|push*indefinite*|park|backlog|indefinite) status="later" ;;
      no|n|skip*|not\ done|delete|remove|drop) status="delete" ;;
      *) echo "  skipping line (unrecognized status): $line" >&2; continue ;;
    esac
    local task
    task=$(jq -r ".tasks[$((idx - 1))] // empty" "$f")
    if [ -z "$task" ]; then
      echo "  skipping #$idx (no task at that index)" >&2
      continue
    fi
    jq --arg task "$task" --arg status "$status" \
      '.statuses[$task] = $status' \
      "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    count=$((count + 1))
    echo "  ✓ #$idx → $status: $task"
  done <<< "$input"

  rebuild_brain
  echo "✓ Applied $count status updates to $target_date"
}

cmd_mark() {
  local idx="${1:?Usage: mark <index> <status>}"
  local status="${2:?Usage: mark <index> <yes|tomorrow|later|delete>}"
  local d
  d=$(today)
  local f="$STATE_DIR/$d.json"

  if [ ! -f "$f" ]; then
    echo "No state for $d" >&2
    return 1
  fi

  local task
  task=$(jq -r ".tasks[$((idx - 1))] // empty" "$f")
  if [ -z "$task" ]; then
    echo "No task at index $idx" >&2
    return 1
  fi

  jq --arg task "$task" --arg status "$status" \
    '.statuses[$task] = $status' \
    "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  rebuild_brain
  echo "✓ Marked '$task' as: $status"
}

# -------------------- reads --------------------

cmd_show() {
  local d
  d=$(resolve_date "${1:-today}")
  local f="$STATE_DIR/$d.json"

  if [ ! -f "$f" ]; then
    if [ "$d" = "$(today)" ]; then
      echo "📋 No tasks set for today ($d). Share your list when ready."
    else
      echo "📋 No record for $d."
    fi
    return 0
  fi

  # Pretty Telegram-friendly view
  local tasks_count statuses_json
  tasks_count=$(jq '.tasks | length' "$f")
  statuses_json=$(jq '.statuses' "$f")

  local label
  if [ "$d" = "$(today)" ]; then label="Today"
  elif [ "$d" = "$(yesterday)" ]; then label="Yesterday"
  else label="$d"; fi

  echo "📋 $label — $tasks_count tasks"
  echo

  jq -r --argjson statuses "$statuses_json" '
    .tasks
    | to_entries
    | map(
        . as $e
        | ($statuses[$e.value] // "open") as $st
        | (
            if $st == "yes" then "✅"
            elif $st == "tomorrow" then "➡️"
            elif $st == "later" then "⏸️"
            elif $st == "delete" then "🗑️"
            else "•"
            end
          ) as $emoji
        | "\($emoji) \($e.key + 1). \($e.value)"
      )
    | .[]
  ' "$f"
}

cmd_carryover() {
  local y_d
  y_d=$(yesterday)
  local f="$STATE_DIR/$y_d.json"

  if [ ! -f "$f" ]; then
    return 0
  fi

  local carryover_tasks indefinite_tasks
  carryover_tasks=$(jq -r '.statuses | to_entries[] | select(.value == "tomorrow") | .key' "$f" 2>/dev/null || echo "")
  indefinite_tasks=$(jq -r '.statuses | to_entries[] | select(.value == "later") | .key' "$f" 2>/dev/null || echo "")

  if [ -n "$carryover_tasks" ]; then
    echo "## Backlog for Today (from yesterday)"
    echo "$carryover_tasks" | sed 's/^/ - /'
    echo
  fi

  if [ -n "$indefinite_tasks" ]; then
    echo "## Indefinite Queue"
    echo "$indefinite_tasks" | sed 's/^/ - /'
    echo
  fi
}

cmd_status() {
  local d
  d=$(today)
  local f="$STATE_DIR/$d.json"
  if [ ! -f "$f" ]; then
    echo "No tasks for today ($d)"
    return 0
  fi
  jq . "$f"
}

# -------------------- brain rebuild --------------------

rebuild_brain() {
  local d t_f y_f
  d=$(today)
  t_f="$STATE_DIR/$d.json"
  y_f="$STATE_DIR/$(yesterday).json"

  mkdir -p "$(dirname "$BRAIN_PAGE_PATH")"

  {
    echo "---"
    echo "type: habit"
    echo "title: Daily Tasks"
    echo "name: Daily Tasks"
    echo "updated: $d"
    echo "tags: [daily, productivity]"
    echo "---"
    echo
    echo "# Daily Tasks"
    echo
    echo "Auto-rebuilt by \`~/bin/daily-tasks.sh\`. Source of truth is"
    echo "\`~/.brain-bootstrap-raw/state/daily-tasks/<date>.json\`."
    echo
    echo "## Today ($d)"
    echo

    if [ -f "$t_f" ]; then
      local count
      count=$(jq '.tasks | length' "$t_f")
      echo "$count tasks set."
      echo
      jq -r '
        .statuses as $st
        | .tasks
        | to_entries
        | map(
            ($st[.value] // "open") as $s
            | (if $s == "yes" then "✅"
               elif $s == "tomorrow" then "➡️"
               elif $s == "later" then "⏸️"
               elif $s == "delete" then "🗑️"
               else "•" end) as $e
            | "- \($e) \(.value)"
          )
        | .[]
      ' "$t_f"
    else
      echo "_No tasks set for today yet._"
    fi
    echo

    if [ -f "$y_f" ]; then
      echo "## Yesterday ($(yesterday)) — recap"
      echo
      jq -r '
        .statuses as $st
        | .tasks
        | map(
            ($st[.] // "open") as $s
            | (if $s == "yes" then "✅"
               elif $s == "tomorrow" then "➡️"
               elif $s == "later" then "⏸️"
               elif $s == "delete" then "🗑️"
               else "•" end) as $e
            | "- \($e) \(.)"
          )
        | .[]
      ' "$y_f"
      echo
    fi

    echo "## How to interact"
    echo
    echo "Tell Alfred:"
    echo "- \"my tasks today: A, B, C\" → replaces the list"
    echo "- \"add D to my list\" → appends"
    echo "- \"remove #2\" or \"delete X\" → removes"
    echo "- \"I finished #1\" → marks done"
    echo "- \"share today's tasks\" → reads via \`daily-tasks.sh show\`"
  } > "$BRAIN_PAGE_PATH"

  # Push into gbrain (file path -> page)
  if [ -x "$GBRAIN" ]; then
    "$GBRAIN" put "$BRAIN_SLUG" < "$BRAIN_PAGE_PATH" >/dev/null 2>&1 || true
  fi

  # Commit to ~/.brain git so backups capture changes
  if [ -d "$BRAIN_DIR/.git" ]; then
    (
      cd "$BRAIN_DIR"
      git add habits/daily-tasks.md 2>/dev/null
      if ! git diff --cached --quiet 2>/dev/null; then
        git -c user.email='alfred@bot' -c user.name='Alfred' \
          commit -m "daily-tasks: state update $d" >/dev/null 2>&1 || true
      fi
    )
  fi
}

# -------------------- dispatch --------------------

case "${1:-help}" in
  set)
    shift
    cmd_set "$@"
    ;;
  add)
    shift
    cmd_add "$@"
    ;;
  remove|rm|delete)
    shift
    cmd_remove "$@"
    ;;
  mark)
    cmd_mark "$2" "$3"
    ;;
  review)
    shift
    cmd_review "$@"
    ;;
  show|today|list)
    cmd_show "${2:-today}"
    ;;
  carryover)
    cmd_carryover
    ;;
  status)
    cmd_status
    ;;
  rebuild-brain)
    rebuild_brain
    echo "✓ Rebuilt $BRAIN_PAGE_PATH"
    ;;
  *)
    cat << 'HELP'
Usage: daily-tasks.sh <command> [args]

Mutations:
  set <task1> <task2>...    Replace today's list (accepts stdin if no args)
  add <task1>...            Append to today's list (accepts stdin)
  remove <index|text>       Delete a task by index or substring match
  mark <index> <status>     yes | tomorrow | later | delete

Reads:
  show [date]               Today (default), yesterday, YYYY-MM-DD, or -N
  carryover                 Yesterday→today + indefinite (for morning briefing)
  status                    Raw JSON for debugging
  rebuild-brain             Force rebuild of habits/daily-tasks brain page
HELP
    ;;
esac
