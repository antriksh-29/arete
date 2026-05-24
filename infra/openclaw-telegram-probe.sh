#!/usr/bin/env bash
# openclaw-telegram-probe.sh — read-only diagnostic.
#
# Purpose: discover what knobs OpenClaw exposes for the Telegram channel
# so we can wire infra/telegram-relay.py (Fix B in docs/TODOS-relay-discipline.md)
# without guessing. Run this once on the VPS; paste the output back into
# the chat with @claude.
#
# Safety: read-only. Doesn't restart services, doesn't write files, doesn't
# leak credentials. All API keys / tokens / bot tokens are redacted before
# printing. Writes its report to stdout AND to /tmp/openclaw-telegram-probe.md
# so you can copy-paste either way.
#
# Usage (on the VPS):
#   bash ~/personal-assistant/infra/openclaw-telegram-probe.sh
#
# Then either:
#   - cat /tmp/openclaw-telegram-probe.md  # if you prefer to copy from file
#   - the report also streams to stdout    # if you ran it interactively

set -uo pipefail

OUT=/tmp/openclaw-telegram-probe.md
exec > >(tee "$OUT")

redact_secrets() {
  # Strip likely-secret values out of JSON / env output. Keeps keys
  # visible but replaces values that look like tokens.
  sed -E '
    s/("(apiKey|api_key|token|secret|password|bearer|authorization|TELEGRAM_BOT_TOKEN|MOONSHOT_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|TELEGRAM_USER_ID)"[[:space:]]*:[[:space:]]*")[^"]*"/\1<REDACTED>"/gi;
    s/([A-Z_]*TOKEN[A-Z_]*=)[^[:space:]]+/\1<REDACTED>/g;
    s/([A-Z_]*API_KEY[A-Z_]*=)[^[:space:]]+/\1<REDACTED>/g;
    s/([A-Z_]*SECRET[A-Z_]*=)[^[:space:]]+/\1<REDACTED>/g;
    s/(Bearer[[:space:]]+)[A-Za-z0-9._\/+=-]+/\1<REDACTED>/g;
    s/(bot)[0-9]+:[A-Za-z0-9_-]+/\1<REDACTED>/g;
  '
}

section() { echo; echo "## $1"; echo; }

echo "# openclaw-telegram-probe report"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"
echo
echo "This is read-only diagnostic output. Secrets are redacted. Paste"
echo "back to inform Fix B / infra/telegram-relay.py integration."

section "1. OpenClaw version + binary location"
which openclaw 2>&1 || echo "(openclaw not on PATH)"
openclaw --version 2>&1 | head -5 || echo "(openclaw --version failed)"

section "2. OpenClaw top-level CLI surface"
openclaw --help 2>&1 | head -80 || echo "(--help failed)"

section "3. OpenClaw config-related subcommands"
echo "### openclaw config --help (if it exists):"
openclaw config --help 2>&1 | head -60 || echo "(no 'config' subcommand)"
echo
echo "### openclaw channels --help (if it exists):"
openclaw channels --help 2>&1 | head -60 || echo "(no 'channels' subcommand)"
echo
echo "### openclaw doctor --help (if it exists):"
openclaw doctor --help 2>&1 | head -60 || echo "(no 'doctor' subcommand)"

section "4. Current channels.telegram subtree (REDACTED)"
if [ -f "$HOME/.openclaw/openclaw.json" ]; then
  if command -v jq >/dev/null 2>&1; then
    jq '.channels.telegram // "(no channels.telegram key)"' "$HOME/.openclaw/openclaw.json" 2>&1 | redact_secrets
  else
    echo "(jq not available — falling back to grep)"
    grep -A 30 '"telegram"' "$HOME/.openclaw/openclaw.json" | head -40 | redact_secrets
  fi
else
  echo "(no ~/.openclaw/openclaw.json found)"
fi

section "5. All top-level keys in openclaw.json"
if [ -f "$HOME/.openclaw/openclaw.json" ] && command -v jq >/dev/null 2>&1; then
  jq 'keys' "$HOME/.openclaw/openclaw.json" 2>&1
  echo
  echo "### channels subtree keys:"
  jq '.channels | keys?' "$HOME/.openclaw/openclaw.json" 2>&1
  echo
  echo "### messages subtree (REDACTED, may inform output-discipline knobs):"
  jq '.messages // "(no messages key)"' "$HOME/.openclaw/openclaw.json" 2>&1 | redact_secrets
  echo
  echo "### agents.defaults keys (excluding model providers):"
  jq '.agents.defaults | keys?' "$HOME/.openclaw/openclaw.json" 2>&1
fi

section "6. Systemd unit env + ExecStart for openclaw-gateway"
echo "### unit file:"
systemctl --user cat openclaw-gateway.service 2>&1 | head -40 | redact_secrets
echo
echo "### env vars systemd sets (names only, no values):"
systemctl --user show openclaw-gateway.service -p Environment 2>&1 | \
  tr ' ' '\n' | grep -oE '^[A-Z_]+=' | sort -u

section "7. Telegram-related strings in the openclaw binary"
echo "(looking for likely config-knob and env-var names)"
OPENCLAW_BIN=$(which openclaw 2>/dev/null)
if [ -n "$OPENCLAW_BIN" ] && command -v strings >/dev/null 2>&1; then
  # Resolve symlinks to the real binary
  REAL_BIN=$(readlink -f "$OPENCLAW_BIN" 2>/dev/null || echo "$OPENCLAW_BIN")
  echo "### Binary: $REAL_BIN ($(stat -c %s "$REAL_BIN" 2>/dev/null || echo '?') bytes)"
  echo
  echo "### Telegram-related identifiers:"
  strings "$REAL_BIN" 2>/dev/null | \
    grep -iE '^(telegram|api\.telegram|TELEGRAM_)' | \
    grep -ivE '^(telegram-bot|telegrambot|telegraf|telegram\.org/[a-z])' | \
    sort -u | head -60
  echo
  echo "### Plausible knob names (apiBaseUrl / baseUrl / endpoint / relay / stream):"
  strings "$REAL_BIN" 2>/dev/null | \
    grep -E '(apiBaseUrl|api_base_url|baseUrl|base_url|endpoint|relayPolicy|relay_policy|streamAssistant|forwardIntermediate|suppressThinking|messageBatch|batchPolicy)' | \
    sort -u | head -40
  echo
  echo "### Channel-related identifiers:"
  strings "$REAL_BIN" 2>/dev/null | \
    grep -E '(channels\.|"channel":|channel_type|channelKind|ChannelConfig)' | \
    sort -u | head -30
else
  echo "(strings or openclaw binary not available)"
fi

section "8. Live process environment (names only, redacted)"
GATEWAY_PID=$(systemctl --user show openclaw-gateway -p MainPID --value 2>/dev/null)
if [ -n "$GATEWAY_PID" ] && [ "$GATEWAY_PID" != "0" ] && [ -r "/proc/$GATEWAY_PID/environ" ]; then
  echo "### env var names openclaw-gateway is running with:"
  tr '\0' '\n' < "/proc/$GATEWAY_PID/environ" | \
    grep -oE '^[A-Z_]+(_API_KEY|_TOKEN|TELEGRAM|API_BASE|BASE_URL|OPENCLAW)' | \
    sort -u
else
  echo "(openclaw-gateway not running or /proc not readable)"
fi

section "9. Recent journal entries that mention telegram or sendMessage"
journalctl --user -u openclaw-gateway -n 200 --no-pager 2>/dev/null | \
  grep -iE 'telegram|sendmessage|chat_id|webhook' | \
  head -20 | \
  redact_secrets || echo "(no journal access)"

section "10. Outbound test — does openclaw use a configurable baseUrl?"
echo "Check if the Moonshot proxy pattern (channels.<provider>.baseUrl)"
echo "applies to Telegram. Look in:"
echo
if [ -f "$HOME/.openclaw/openclaw.json" ] && command -v jq >/dev/null 2>&1; then
  echo "### Any *.baseUrl fields anywhere in openclaw.json:"
  jq -r 'paths(.baseUrl? // empty) | join(".")' "$HOME/.openclaw/openclaw.json" 2>&1 | sort -u
  echo
  echo "### Any *.apiBaseUrl fields:"
  jq -r 'paths(.apiBaseUrl? // empty) | join(".")' "$HOME/.openclaw/openclaw.json" 2>&1 | sort -u
fi

echo
echo "## Done"
echo
echo "Report saved to: $OUT"
echo "Size: $(wc -l < "$OUT") lines, $(wc -c < "$OUT") bytes"
echo
echo "Paste the content back into the chat. The next step is to design"
echo "telegram-relay.py based on whichever of these knobs actually exist."
