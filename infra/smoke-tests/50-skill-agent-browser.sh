#!/usr/bin/env bash
# Smoke test for the agent-browser skill. Runs --smoke against the
# deployed local wrapper; the actual skill body is OpenClaw-hosted.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "agent-browser" "bash" "$HOME/bin/agent-browser-status.sh"
