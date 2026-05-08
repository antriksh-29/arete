#!/usr/bin/env bash
# Smoke test for the image-ocr skill. Runs --smoke against the deployed
# script. Drops into ~/.gbrain/smoke-tests.d/ via infra/sync-smoke-tests.sh.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "image-ocr" "python3" "$HOME/bin/image-ocr.py"
