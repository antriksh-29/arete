#!/usr/bin/env bash
# Smoke test for the weather skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "weather" "python3" "$HOME/bin/weather-snapshot.py"
