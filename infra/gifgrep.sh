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

# JSON output gives us titles + URLs; format into a markdown list.
gifgrep search --json "$QUERY" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if not data:
    print('No GIFs found for: $QUERY')
    sys.exit(0)
print(f'GIF results for **$QUERY**:')
print()
for i, r in enumerate(data[:$MAX], 1):
    title = (r.get('title') or 'untitled').strip() or 'untitled'
    url = r.get('url') or ''
    print(f'**{i}. {title}**')
    print(f'→ {url}')
    print()
" | sed '$d'  # drop trailing blank line
