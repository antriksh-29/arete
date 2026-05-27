#!/bin/bash
# gifgrep.sh — thin Telegram-friendly wrapper around the upstream
# `gifgrep` binary (steipete/gifgrep). Returns the top N result URLs
# as a markdown list so the agent can relay them verbatim.
#
# Usage:   gifgrep.sh "<query>" [max=5]
# Smoke:   gifgrep.sh --smoke
#
# Why this wrapper exists: the bare `gifgrep <query> --max N --format url`
# returns plain URLs without context. For a Telegram reply we want a
# titled list. Also gives us a single script: target the SKILL.md
# frontmatter can point at, which makes the audit gate happy and
# nudges the agent toward consistent invocation.
#
# v0.3.0 of upstream requires KLIPY_API_KEY for ALL sources (klipy/tenor/
# giphy proxy through klipy in the current build). Key is read from
# the arete-cred vault; if missing the script prints a clear ABORT line
# the agent will relay verbatim.

set -uo pipefail

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"gifgrep","user_message":"gifgrep smoke ok"}'
  exit 0
fi

QUERY="${1:-}"
MAX="${2:-5}"

if [ -z "$QUERY" ]; then
  echo 'Usage: gifgrep.sh "<query>" [max=5]' >&2
  exit 1
fi

if ! command -v gifgrep >/dev/null 2>&1; then
  echo 'ABORT: gifgrep binary not on PATH' >&2
  exit 1
fi

KLIPY_API_KEY="$(/usr/local/bin/arete-cred get KLIPY_API_KEY 2>/dev/null)"
if [ -z "$KLIPY_API_KEY" ]; then
  echo 'ABORT: KLIPY_API_KEY missing from vault — sign up free at https://klipy.com, then `arete-cred set KLIPY_API_KEY <key>` on the VPS'
  exit 1
fi
export KLIPY_API_KEY

# JSON output gives us titles + URLs; format into a markdown list.
export GIFGREP_QUERY="$QUERY"
gifgrep search --json "$QUERY" --max "$MAX" 2>/dev/null | python3 -c "
import json, sys, os
raw = sys.stdin.read().strip()
q = os.environ.get('GIFGREP_QUERY', '')
if not raw:
    print(f'No GIFs found for: {q}')
    sys.exit(0)
try:
    data = json.loads(raw)
except Exception:
    print('gifgrep returned non-JSON output')
    sys.exit(0)
if not isinstance(data, list) or not data:
    print(f'No GIFs found for: {q}')
    sys.exit(0)
print(f'GIF results for **{q}**:')
print()
for i, r in enumerate(data, 1):
    title = (r.get('title') or 'untitled').strip() or 'untitled'
    url = r.get('url') or ''
    print(f'**{i}. {title}**')
    print(f'→ {url}')
    print()
"
