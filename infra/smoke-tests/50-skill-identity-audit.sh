#!/usr/bin/env bash
# Smoke test for the identity-audit skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "identity-audit" "python3" "$HOME/bin/identity-audit.py"
