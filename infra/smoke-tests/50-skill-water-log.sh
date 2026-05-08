#!/usr/bin/env bash
# Smoke test for the water-log skill. Runs --smoke against the deployed
# script. Drops into ~/.gbrain/smoke-tests.d/ via infra/sync-smoke-tests.sh.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "water-log" "bash" "$HOME/bin/water-log.sh"
