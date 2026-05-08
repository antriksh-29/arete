#!/usr/bin/env bash
# agent-browser-status.sh — thin local wrapper. The actual agent-browser
# skill is OpenClaw-hosted (community skill, installed via
# `openclaw skill install agent-browser`). This wrapper exists so the
# audit gate has a script: target it can verify.
#
# Subcommands:
#   --smoke     cold-boot callability check (no openclaw call)
#   status      report whether OpenClaw has the skill installed
#   soak-status print the read-only-soak end date and whether soak is over

set -uo pipefail

SOAK_END="2026-05-26"

case "${1:-status}" in
  --smoke)
    echo '{"ok":true,"skill":"agent-browser","user_message":"agent-browser smoke ok"}'
    exit 0
    ;;
  status)
    if command -v openclaw >/dev/null 2>&1 && openclaw skill list 2>/dev/null | grep -q "^agent-browser"; then
      echo '{"ok":true,"skill":"agent-browser","installed":true,"host":"openclaw","user_message":"agent-browser registered with OpenClaw"}'
      exit 0
    else
      echo '{"ok":false,"skill":"agent-browser","installed":false,"host":"openclaw","user_message":"agent-browser NOT registered — run: openclaw skill install agent-browser"}'
      exit 1
    fi
    ;;
  soak-status)
    today=$(TZ=Asia/Kolkata date +%Y-%m-%d)
    if [[ "$today" < "$SOAK_END" ]]; then
      remaining=$(( ($(date -j -f %Y-%m-%d "$SOAK_END" +%s 2>/dev/null || date -d "$SOAK_END" +%s) - $(date +%s)) / 86400 ))
      echo "{\"ok\":true,\"skill\":\"agent-browser\",\"soak_active\":true,\"soak_ends\":\"$SOAK_END\",\"days_remaining\":$remaining,\"user_message\":\"agent-browser is in read-only soak — clicks/types disabled until $SOAK_END (${remaining} days remaining); soak ends only after AGENTS.md catches ≥1 real-world prompt-injection attempt\"}"
    else
      echo "{\"ok\":true,\"skill\":\"agent-browser\",\"soak_active\":false,\"soak_ends\":\"$SOAK_END\",\"user_message\":\"agent-browser soak window has elapsed; click/type capability gated on AGENTS.md catching ≥1 real-world injection (verify before enabling)\"}"
    fi
    exit 0
    ;;
  *)
    echo "{\"ok\":false,\"user_message\":\"unknown subcommand: $1 (try: status, soak-status, --smoke)\"}" >&2
    exit 2
    ;;
esac
