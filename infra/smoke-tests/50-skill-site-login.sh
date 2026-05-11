#!/usr/bin/env bash
# Smoke test for the site-login skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "site-login" "python3" "$HOME/bin/browser-login.py"
