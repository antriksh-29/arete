#!/usr/bin/env bash
# Smoke test for the x-draft skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "x-draft" "python3" "$HOME/bin/x-draft.py"
