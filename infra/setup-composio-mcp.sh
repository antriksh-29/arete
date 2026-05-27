#!/bin/bash
# setup-composio-mcp.sh — wire up Composio as an MCP server backing
# Arete's tool catalog. Composio exposes 100+ app integrations (Gmail,
# Calendar, Slack, Notion, Linear, HubSpot, etc.) through one MCP
# endpoint at https://connect.composio.dev/mcp.
#
# Usage:
#   bash setup-composio-mcp.sh <composio-api-key>
#   bash setup-composio-mcp.sh --check        # show current state, no writes
#
# Run this on the VPS (agent). It:
#   1. Stores COMPOSIO_API_KEY in the arete-cred vault (audit trail + future
#      reference)
#   2. Writes the key into mcp.servers.composio.headers.x-consumer-api-key
#      in ~/.openclaw/openclaw.json. OpenClaw doesn't interpolate env vars
#      into mcp.servers headers, so the key must live inline (same plaintext
#      pattern as the moonshot apiKey already in this file; mode 600).
#   3. Restarts openclaw-gateway and verifies the composio MCP server boots
#      without the prior "Missing authentication" error.
#
# Composio's setup prompt says "OAuth will be used automatically" but that
# only applies to their CLI/desktop clients. HTTP-only MCP clients like
# OpenClaw require the x-consumer-api-key header.

set -uo pipefail

if [ "${1:-}" = "--check" ]; then
  echo "=== current composio MCP config ==="
  openclaw mcp show composio 2>&1 | head -20
  echo ""
  echo "=== vault slot ==="
  if arete-cred list 2>&1 | grep -q "^COMPOSIO_API_KEY "; then
    echo "  COMPOSIO_API_KEY: present"
  else
    echo "  COMPOSIO_API_KEY: missing"
  fi
  echo ""
  echo "=== last 5 gateway log lines mentioning composio ==="
  journalctl --user -u openclaw-gateway.service --since "10 minutes ago" --no-pager 2>&1 | grep -i composio | tail -5
  exit 0
fi

KEY="${1:-}"
if [ -z "$KEY" ]; then
  echo "Usage: setup-composio-mcp.sh <composio-api-key>"
  echo "       setup-composio-mcp.sh --check"
  echo ""
  echo "Get the API key from https://dashboard.composio.dev → Settings → API Keys"
  exit 1
fi

# Sanity: composio keys are typically 30-80 chars, alphanumeric + dashes
if [ ${#KEY} -lt 20 ] || [ ${#KEY} -gt 200 ]; then
  echo "WARN: API key length is ${#KEY} — that looks wrong. Continue anyway? (Ctrl-C to abort)"
  read -r _
fi

echo "=== 1. store key in arete-cred vault ==="
echo "$KEY" | arete-cred set COMPOSIO_API_KEY --value-stdin 2>&1 | tail -3 \
  || { echo "  arete-cred set failed; trying interactive..."; arete-cred set COMPOSIO_API_KEY; }

echo ""
echo "=== 2. write mcp.servers.composio.headers.x-consumer-api-key into openclaw.json ==="
KEY_VAL="$KEY" python3 - <<'PYEOF'
import json, os, pathlib, shutil, datetime
p = pathlib.Path.home() / ".openclaw/openclaw.json"
cfg = json.loads(p.read_text())
srv = cfg.setdefault("mcp", {}).setdefault("servers", {}).setdefault("composio", {})
srv["url"] = "https://connect.composio.dev/mcp"
srv["transport"] = "streamable-http"
hdrs = srv.setdefault("headers", {})
hdrs["x-consumer-api-key"] = os.environ["KEY_VAL"]
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy(p, p.with_suffix(p.suffix + ".bak." + stamp))
p.write_text(json.dumps(cfg, indent=2))
print(f"  wrote mcp.servers.composio with headers (backup .bak.{stamp})")
PYEOF

echo ""
echo "=== 3. restart gateway ==="
systemctl --user restart openclaw-gateway.service
sleep 6
echo "  status: $(systemctl --user is-active openclaw-gateway.service)"

echo ""
echo "=== 4. verify composio boots clean ==="
journalctl --user -u openclaw-gateway.service --since "20 seconds ago" --no-pager 2>&1 \
  | grep -iE "composio|bundle-mcp.*composio" | head -10

echo ""
echo "If you see 'failed to start server composio' above, the key is wrong."
echo "If silent, composio MCP is connected — try in Telegram:"
echo "  > what apps does composio expose"
echo "  > use composio to search my recent Gmail"
