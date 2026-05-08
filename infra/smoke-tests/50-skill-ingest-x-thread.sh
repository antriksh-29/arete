#!/usr/bin/env bash
# Smoke test for the ingest-x-thread skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "ingest-x-thread" "python3" "$HOME/bin/ingest-x-thread.py"
