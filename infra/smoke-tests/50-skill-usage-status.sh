#!/usr/bin/env bash
# Smoke test for the usage-status skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "usage-status" "python3" "$HOME/bin/usage-status.py"
