#!/usr/bin/env bash
# Smoke test for the reading-list skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "reading-list" "python3" "$HOME/bin/reading-list.py"
