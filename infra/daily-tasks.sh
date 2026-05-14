#!/bin/bash
# Thin wrapper around daily-tasks.py (v2). Keeps the existing
# ~/bin/daily-tasks.sh path stable so cron units, AGENTS.md, SKILL.md
# and the bot's tool calls don't need to change.
#
# All semantics, exit codes, and argument shapes live in daily-tasks.py.
# That includes the B1 regression guards: `review` exits 2 on no input
# and 3 on no parseable status lines.
set -uo pipefail

# Resolve daily-tasks.py: prefer the deployed copy in ~/bin, fall back
# to the repo for local invocations.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for cand in "$HOME/bin/daily-tasks.py" "$SCRIPT_DIR/daily-tasks.py"; do
  if [ -f "$cand" ]; then
    exec python3 "$cand" "$@"
  fi
done

echo "ERROR: daily-tasks.py not found in ~/bin or alongside this wrapper" >&2
exit 127
