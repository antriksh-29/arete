#!/usr/bin/env bash
# Smoke test for say.sh — the single allowed user-facing send path
# under messages.visibleReplies=message_tool (see RULE 0.7).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "say" "bash" "$HOME/bin/say.sh"
