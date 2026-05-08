#!/usr/bin/env bash
# Smoke test for the humanizer skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "humanizer" "python3" "$HOME/bin/humanizer.py"
