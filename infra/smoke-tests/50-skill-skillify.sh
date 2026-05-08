#!/usr/bin/env bash
# Smoke test for the skillify skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "skillify" "python3" "$HOME/bin/skillify.py"
