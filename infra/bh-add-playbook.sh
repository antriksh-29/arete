#!/bin/bash
# bh-add-playbook.sh — write a new browser-harness domain-skill playbook
# captured from a real task we just solved.
#
# Usage:
#   bh-add-playbook.sh <site-slug> <workflow-slug> "<body-or-path-to-md>"
#
# Examples:
#   bh-add-playbook.sh notion compose-page "$(cat /tmp/notion-quirk.md)"
#   bh-add-playbook.sh bookmyshow seat-pick /tmp/bms-playbook.md
#
# The body should follow the shape documented in
# skills/agent-browser/SKILL.md §"capture as a domain-skill":
#   - URL patterns
#   - Private APIs
#   - Stable selectors
#   - Framework quirks
#   - Waits the auto-wait misses
#   - Traps
#
# Lives at ~/.browser-harness/repo/domain-skills/<site>/<workflow>.md
# on the local-additions branch. Auto-commits with a structured
# message so future agents (and the git log) can see the accreted
# knowledge.

set -uo pipefail

if [ $# -lt 3 ]; then
  echo "Usage: bh-add-playbook.sh <site-slug> <workflow-slug> <body-or-file>" >&2
  echo "  site-slug:     lowercase, no dots, e.g. 'notion', 'bookmyshow', 'zomato'" >&2
  echo "  workflow-slug: lowercase, hyphenated, e.g. 'compose-page', 'seat-pick'" >&2
  echo "  body:          either a markdown string OR a path to a .md file" >&2
  exit 2
fi

SITE="$1"
WORKFLOW="$2"
BODY_INPUT="$3"

REPO="${BH_REPO:-$HOME/.browser-harness/repo}"
SKILL_DIR="$REPO/domain-skills/$SITE"
SKILL_FILE="$SKILL_DIR/$WORKFLOW.md"

if [ ! -d "$REPO/.git" ]; then
  echo "ABORT: browser-harness clone not found at $REPO" >&2
  exit 1
fi

# Resolve body: file path or literal text
BODY=""
if [ -f "$BODY_INPUT" ]; then
  BODY=$(cat "$BODY_INPUT")
else
  BODY="$BODY_INPUT"
fi

if [ ${#BODY} -lt 60 ]; then
  echo "ABORT: body too short (${#BODY} chars). Playbooks need real content." >&2
  echo "       follow the shape in skills/agent-browser/SKILL.md §domain-skills" >&2
  exit 1
fi

# Reject obviously bad content: pixel coords, secrets, run narration
if echo "$BODY" | grep -qiE '\b(click_at_xy\([0-9]+,\s*[0-9]+\)|first I clicked|then I typed|cookie:.*[a-zA-Z0-9]{16})\b'; then
  echo "ABORT: body looks like run-narration or secrets. Playbooks are maps, not diaries." >&2
  exit 1
fi

mkdir -p "$SKILL_DIR"

# Ensure we are on the local-additions branch (idempotent)
cd "$REPO"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "local-additions" ]; then
  if git rev-parse --verify local-additions >/dev/null 2>&1; then
    git checkout local-additions
  else
    git checkout -b local-additions
  fi
fi

# Idempotency: if file exists with identical content, no-op.
if [ -f "$SKILL_FILE" ] && [ "$(cat "$SKILL_FILE")" = "$BODY" ]; then
  echo "✓ playbook already exists with identical content — no-op"
  echo "  $SKILL_FILE"
  exit 0
fi

# Prepend a small header so we can audit who wrote it + when
TIMESTAMP=$(date -Iseconds)
{
  echo "<!-- captured by personal-assistant bot, $TIMESTAMP -->"
  echo ""
  echo "$BODY"
} > "$SKILL_FILE"

git add "$SKILL_FILE"

if git diff --cached --quiet; then
  echo "✓ no change after normalize — no-op"
  exit 0
fi

git commit -m "domain-skills/$SITE/$WORKFLOW: capture playbook

Auto-captured by the personal-assistant agent after solving a
real task on $SITE. See timestamp in file header.

Path:  domain-skills/$SITE/$WORKFLOW.md
Bytes: $(stat -c%s "$SKILL_FILE")
" 2>&1 | tail -3

echo ""
echo "✓ wrote $SKILL_FILE ($(stat -c%s "$SKILL_FILE") bytes)"
echo "  branch: local-additions"
echo "  audit:  git -C $REPO log --oneline -5"
