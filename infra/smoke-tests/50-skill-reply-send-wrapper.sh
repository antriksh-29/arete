#!/usr/bin/env bash
# Smoke test for reply-send-wrapper. Note: this is infrastructure
# (no SKILL.md), but we still want it in the smoke loop because it's
# called on the hot path of every outbound reply.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_lib.sh"
run_smoke "reply-send-wrapper" "bash" "$HOME/bin/reply-send-wrapper.sh"
