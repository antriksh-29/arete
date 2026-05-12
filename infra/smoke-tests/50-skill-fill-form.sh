#!/usr/bin/env bash
# Smoke test for the fill-form skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "fill-form" "python3" "$HOME/bin/fill-form.py"
