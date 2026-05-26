#!/usr/bin/env bash
# openclaw-telegram-probe-v9.sh — read-only investigation for Bug I
# (signal-detector hangs + main-session context overflow).
#
# Gathers data needed to evaluate whether the other-Claude compaction-
# config recipe applies to our openclaw 2026.5.12 + whether signal-
# detector hangs need their own separate fix.
#
# Read-only. Safe. Output → stdout + /tmp/probe-v9.md

set -uo pipefail
OUT=/tmp/probe-v9.md
exec > >(tee "$OUT")
section() { echo; echo "## $1"; echo; }

echo "# probe-v9 — Bug I deep-dive"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. Embedded runner vs gateway runner"
echo "### Which mode is openclaw running in?"
ps -ef | grep -E 'openclaw.*gateway|openclaw.*pi |openclaw.*embedded' | grep -v grep | head -5
echo
echo "(if 'gateway' appears → bug #67473 about embedded-runner auto-compaction doesn't apply.)"

section "2. Current compaction config — what's actually set"
openclaw config get agents.defaults.compaction 2>&1 | head -40
echo
echo "### top-level mode setting"
openclaw config get agents.defaults.compaction.mode 2>&1

section "3. Compaction schema — do the proposed knobs exist?"
SCHEMA=$(mktemp)
openclaw config schema > "$SCHEMA" 2>/dev/null

for key in \
  "triggerTokens" \
  "keepRecentTokens" \
  "reserveTokens" \
  "midTurnPrecheck" \
  "memoryFlush" \
  "softThresholdTokens" \
  "postCompactionSections" \
  "notifyUser" \
  "truncateAfterCompaction" \
  "maxActiveTranscriptBytes" \
  "mode"; do
  echo "### compaction.${key}:"
  jq ".properties.agents.properties.defaults.properties.compaction.properties.${key} // \"(missing from schema)\"" "$SCHEMA" 2>&1 | head -8
  echo
done

echo "### All keys under agents.defaults.compaction:"
jq '.properties.agents.properties.defaults.properties.compaction.properties | keys' "$SCHEMA" 2>&1

section "4. Kimi K2.6 — context-window setting"
openclaw config get 'agents.defaults.models."moonshot/kimi-k2.6"' 2>&1 | head -20
echo
echo "### Specifically the contextTokens / maxInputTokens / contextWindow setting"
openclaw config get 'agents.defaults.models."moonshot/kimi-k2.6".contextTokens' 2>&1
echo
openclaw config get 'agents.defaults.models."moonshot/kimi-k2.6".context1m' 2>&1

section "5. Signal-detector hang patterns — last 24h"
echo "### count of recent timeouts vs successes:"
journalctl --user -u openclaw-gateway --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -E '\[agent/embedded\] \[compaction-diag\] end' | \
  grep -oE 'outcome=[a-z]+ reason=[a-z_]+' | \
  sort | uniq -c | sort -rn | head -10

echo
echo "### timeout duration distribution (sample):"
journalctl --user -u openclaw-gateway --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -E '\[compaction-diag\] end.*outcome=failed' | \
  grep -oE 'durationMs=[0-9]+' | \
  sort -t= -k2 -n | uniq -c | tail -15

echo
echo "### specific error reasons (top 10):"
journalctl --user -u openclaw-gateway --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -iE 'compaction|signal-detector|session file locked|Summarization' | \
  grep -oE 'detail=[a-zA-Z_:0-9 ]{1,80}' | \
  sort | uniq -c | sort -rn | head -10

echo
echo "### session-locking errors specifically:"
journalctl --user -u openclaw-gateway --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -c 'session file locked' 2>/dev/null
echo "(occurrences in last 24h ↑)"

section "6. Moonshot proxy state"
systemctl --user is-active moonshot-proxy.service 2>&1
echo
echo "### proxy journal — circuit-breaker events in last 24h:"
journalctl --user -u moonshot-proxy --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -iE 'circuit|breaker|overload|429|fallback|fail' | \
  tail -20

section "7. Current active session sizes"
SESSIONS_DIR="$HOME/.openclaw/agents/main/sessions"
echo "### main-agent active session (lines + bytes):"
active_main=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | grep -v -E '\.(trajectory|reset|checkpoint)\.' | head -1)
if [ -n "$active_main" ]; then
  bytes=$(stat -c %s "$active_main" 2>/dev/null || stat -f %z "$active_main" 2>/dev/null)
  lines=$(wc -l < "$active_main" 2>/dev/null)
  est_tokens=$(( bytes / 4 ))
  echo "$(basename "$active_main"): $lines lines, $bytes bytes (~$est_tokens tokens est)"
else
  echo "(none)"
fi

echo
echo "### signal-detector active session:"
SD_SESSIONS_DIR="$HOME/.openclaw/agents/signal-detector/sessions"
active_sd=$(ls -t "$SD_SESSIONS_DIR"/*.jsonl 2>/dev/null | grep -v -E '\.(trajectory|reset|checkpoint)\.' | head -1)
if [ -n "$active_sd" ]; then
  bytes=$(stat -c %s "$active_sd" 2>/dev/null || stat -f %z "$active_sd" 2>/dev/null)
  lines=$(wc -l < "$active_sd" 2>/dev/null)
  est_tokens=$(( bytes / 4 ))
  echo "$(basename "$active_sd"): $lines lines, $bytes bytes (~$est_tokens tokens est)"
else
  echo "(none)"
fi

section "8. Stale session lock files (the head-of-line block)"
echo "### main-agent locks (should be 0 when bot is idle):"
ls -la "$SESSIONS_DIR"/*.lock 2>/dev/null | head -5 || echo "(none)"
echo
echo "### signal-detector locks (probe v5 showed these stick around):"
ls -la "$SD_SESSIONS_DIR"/*.lock 2>/dev/null | head -10 || echo "(none)"
echo
echo "(stale locks → next compaction attempt 60s-timeouts → cascade hang)"

section "9. Embedded runner / Pi check (bug #67473 applicability)"
echo "### embeddedHarness setting:"
openclaw config get agents.defaults.embeddedHarness 2>&1
echo
echo "### embeddedPi setting:"
openclaw config get agents.defaults.embeddedPi 2>&1

section "10. Telegram DM history limit (recipe proposes 30)"
openclaw config get channels.telegram 2>&1 | head -20
echo
echo "### specifically dmHistoryLimit / historyLimit:"
openclaw config get channels.telegram.dmHistoryLimit 2>&1
echo
openclaw config get messages.groupChat.historyLimit 2>&1

section "11. Spool depth right now"
SPOOL_DIR="$HOME/.openclaw/telegram/ingress-spool-default"
echo "### spool file count + ages:"
if [ -d "$SPOOL_DIR" ]; then
  count=$(ls -1 "$SPOOL_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
  echo "depth: $count"
  ls -lt "$SPOOL_DIR"/*.json 2>/dev/null | head -5
else
  echo "(no spool dir)"
fi

section "12. Recent watchdog activity"
journalctl --user -u spool-stall-watchdog --since '24 hours ago' --no-pager 2>/dev/null | tail -15

rm -f "$SCHEMA"
echo
echo "## Done"
echo "Report: $OUT ($(wc -l < "$OUT") lines)"
