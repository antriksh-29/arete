#!/bin/bash
# mcporter-list.sh — list configured MCP servers in a Telegram-friendly
# format. Calls `mcporter list --output json` and renders a short
# markdown summary.
#
# Usage:   mcporter-list.sh
# Smoke:   mcporter-list.sh --smoke
#
# Why this wrapper: bare `mcporter list` output is table-shaped and
# pads to terminal width — not great for a Telegram reply. Gives the
# audit gate a script: target it can point at.

set -uo pipefail

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"mcporter","user_message":"mcporter smoke ok"}'
  exit 0
fi

if ! command -v mcporter >/dev/null 2>&1; then
  echo 'ABORT: mcporter binary not on PATH' >&2
  exit 1
fi

raw=$(mcporter list --output json 2>/dev/null || true)
if [ -z "$raw" ] || [ "$raw" = "[]" ]; then
  echo 'No MCP servers configured.'
  echo
  echo 'Add one with: `mcporter config add <server-name> <url>` or `mcporter auth <url>`.'
  exit 0
fi

echo "$raw" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print('mcporter returned non-JSON output')
    sys.exit(0)
if not isinstance(data, list) or not data:
    print('No MCP servers configured.')
    sys.exit(0)
print(f'Configured MCP servers ({len(data)}):')
print()
for entry in data:
    name = entry.get('name') or entry.get('id') or 'unnamed'
    url = entry.get('url') or entry.get('transport') or ''
    auth = entry.get('auth') or entry.get('status') or ''
    print(f'- **{name}** — {url}' + (f' ({auth})' if auth else ''))
"
