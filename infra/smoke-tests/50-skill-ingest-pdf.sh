#!/usr/bin/env bash
# Smoke test for the ingest-pdf skill.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "ingest-pdf" "python3" "$HOME/bin/ingest-pdf.py"
