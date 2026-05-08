#!/usr/bin/env bash
# Smoke test for the activity-tracker skill. Runs --smoke against the deployed
# script. Drops into ~/.gbrain/smoke-tests.d/ via infra/sync-smoke-tests.sh.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "activity-tracker" "bash" "$HOME/bin/activity-tracker.sh"
