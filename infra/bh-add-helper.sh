#!/bin/bash
# bh-add-helper.sh — append a new helper function to the browser-harness
# editable agent_helpers.py, then commit. Used by the agent when it
# discovers a missing primitive while executing a task.
#
# Usage:
#   bh-add-helper.sh <helper-name> "<helper-body>"
#   bh-add-helper.sh <helper-name> /path/to/helper.py
#
# helper-body MUST be the full python def (signature + docstring + body)
# using the harness's pre-imported primitives (cdp, click_at_xy,
# wait_for_load, http_get, etc.).
#
# Example:
#   bh-add-helper.sh clear_cookies_for_domain "
#   def clear_cookies_for_domain(domain: str):
#       \"\"\"Delete all cookies whose domain matches the given suffix.\"\"\"
#       cookies = cdp('Network.getAllCookies').get('cookies', [])
#       for c in cookies:
#           if c['domain'].endswith(domain):
#               cdp('Network.deleteCookies',
#                   name=c['name'], domain=c['domain'], path=c.get('path','/'))
#   "
#
# The file lives at ~/.browser-harness/repo/agent-workspace/agent_helpers.py
# on the local-additions branch.

set -uo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: bh-add-helper.sh <helper-name> <body-or-file>" >&2
  echo "  helper-name: snake_case, e.g. 'clear_cookies_for_domain'" >&2
  echo "  body:        a full Python def block OR a path to a .py file" >&2
  exit 2
fi

NAME="$1"
BODY_INPUT="$2"

REPO="${BH_REPO:-$HOME/.browser-harness/repo}"
HELPERS_FILE="$REPO/agent-workspace/agent_helpers.py"

if [ ! -d "$REPO/.git" ]; then
  echo "ABORT: browser-harness clone not found at $REPO" >&2
  exit 1
fi

# Resolve body: file path or literal
BODY=""
if [ -f "$BODY_INPUT" ]; then
  BODY=$(cat "$BODY_INPUT")
else
  BODY="$BODY_INPUT"
fi

# Sanity checks
if ! echo "$BODY" | grep -qE "^def[[:space:]]+${NAME}\\b"; then
  echo "ABORT: body does not declare 'def $NAME' as the first def. Got:" >&2
  echo "$BODY" | head -3 >&2
  exit 1
fi

# Don't double-add
if grep -qE "^def ${NAME}\\b" "$HELPERS_FILE" 2>/dev/null; then
  echo "ABORT: helper '$NAME' already exists in $HELPERS_FILE" >&2
  echo "  Edit by hand if you need to update; this script appends only." >&2
  exit 1
fi

cd "$REPO"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "local-additions" ]; then
  if git rev-parse --verify local-additions >/dev/null 2>&1; then
    git checkout local-additions
  else
    git checkout -b local-additions
  fi
fi

TIMESTAMP=$(date -Iseconds)
{
  echo ""
  echo ""
  echo "# Added by personal-assistant bot, $TIMESTAMP"
  echo "$BODY"
} >> "$HELPERS_FILE"

git add "$HELPERS_FILE"

if git diff --cached --quiet; then
  echo "✓ no change after normalize — no-op"
  exit 0
fi

git commit -m "agent_helpers: add ${NAME}

Captured by the personal-assistant agent after using this primitive
to complete a task. See file timestamp comment above the def.
" 2>&1 | tail -3

echo ""
echo "✓ appended def ${NAME} → $HELPERS_FILE"
echo "  next browser-harness invocation will see it (editable install)"
echo "  audit: git -C $REPO log --oneline -5"
