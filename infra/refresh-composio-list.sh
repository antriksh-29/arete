#!/usr/bin/env bash
# refresh-composio-list.sh — replace the auto-managed Composio toolkits
# block in AGENTS.md with the live state from `composio-connections.sh`.
# Idempotent; safe to run on every deploy.
#
# Touches BOTH locations so they stay in sync:
#   • ~/personal-assistant/identity/AGENTS.md (the repo source — autosync
#     pushes any changes back to GitHub, so the canonical doc on
#     antriksh-29/personal-assistant always shows the current toolkit list)
#   • ~/.openclaw/workspace/AGENTS.md (what the agent reads at runtime)
#
# Markers in AGENTS.md (Rule 8b):
#   <!-- BEGIN AUTO-MANAGED COMPOSIO TOOLKITS LIST -->
#   ... content ...
#   <!-- END AUTO-MANAGED COMPOSIO TOOLKITS LIST -->
#
# If the markers are missing (someone edited Rule 8b), exit 0 with a
# warning — never corrupt the file.

set -uo pipefail

REPO_AGENTS="${REPO_AGENTS:-$HOME/personal-assistant/identity/AGENTS.md}"
WORKSPACE_AGENTS="${WORKSPACE_AGENTS:-$HOME/.openclaw/workspace/AGENTS.md}"
CONN_SCRIPT="${CONN_SCRIPT:-$HOME/bin/composio-connections.sh}"

if [ ! -x "$CONN_SCRIPT" ]; then
  echo "refresh-composio-list: composio-connections.sh not at $CONN_SCRIPT — skipping"
  exit 0
fi

# Parse composio output → comma-separated toolkit summary via Python
# (em-dashes and other multi-byte chars break awk on this output).
FORMATTED=$(bash "$CONN_SCRIPT" 2>/dev/null | python3 -c '
import re, sys
# Display-name map. Composio slugs are lowercase concatenated; humans
# read multi-word forms. Unmapped slugs use title-case fallback.
display = {
    "gmail": "Gmail",
    "googlecalendar": "Google Calendar",
    "googlesheets": "Google Sheets",
    "googledrive": "Google Drive",
    "googledocs": "Google Docs",
    "youtube": "YouTube",
    "linkedin": "LinkedIn",
    "twitter": "Twitter/X",
    "x": "X (Twitter)",
    "notion": "Notion",
    "slack": "Slack",
    "github": "GitHub",
    "discord": "Discord",
    "asana": "Asana",
    "trello": "Trello",
    "jira": "Jira",
    "linear": "Linear",
    "airtable": "Airtable",
    "hubspot": "HubSpot",
    "salesforce": "Salesforce",
    "stripe": "Stripe",
    "shopify": "Shopify",
    "intercom": "Intercom",
    "zoom": "Zoom",
    "calendly": "Calendly",
    "figma": "Figma",
    "miro": "Miro",
    "dropbox": "Dropbox",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "perplexity": "Perplexity",
}
text = sys.stdin.read()
pairs = []
for line in text.splitlines():
    m = re.match(r"^✅\s+\*\*([^*]+)\*\*\s+[—-]\s+(\d+)\s+account", line)
    if not m:
        continue
    slug, count = m.group(1).strip().lower(), int(m.group(2))
    name = display.get(slug) or slug[:1].upper() + slug[1:]
    if count > 1:
        pairs.append(f"{name} ({count} accounts)")
    else:
        pairs.append(name)
print(", ".join(pairs))
')

if [ -z "$FORMATTED" ]; then
  echo "refresh-composio-list: no toolkits parsed — skipping"
  exit 0
fi

TODAY=$(date +%Y-%m-%d)

# Substitute into a single file via python (multi-line regex)
substitute() {
  local target="$1"
  if [ ! -f "$target" ]; then
    return 0
  fi
  if ! grep -q "BEGIN AUTO-MANAGED COMPOSIO TOOLKITS LIST" "$target"; then
    echo "refresh-composio-list: $target missing markers — skipping"
    return 0
  fi
  AGENTS_TARGET="$target" TOOLKITS="$FORMATTED" TODAY="$TODAY" python3 <<'PYEOF'
import os, re, pathlib
path = os.environ["AGENTS_TARGET"]
toolkits = os.environ["TOOLKITS"]
today = os.environ["TODAY"]
text = pathlib.Path(path).read_text()
new_block = (
    f"<!-- BEGIN AUTO-MANAGED COMPOSIO TOOLKITS LIST -->\n"
    f"{toolkits}. Last refreshed during {today} deploy.\n"
    f"<!-- END AUTO-MANAGED COMPOSIO TOOLKITS LIST -->"
)
pattern = re.compile(
    r"<!-- BEGIN AUTO-MANAGED COMPOSIO TOOLKITS LIST -->.*?<!-- END AUTO-MANAGED COMPOSIO TOOLKITS LIST -->",
    re.S,
)
new_text = pattern.sub(new_block, text)
if new_text == text:
    print(f"refresh-composio-list: {path} already up to date")
else:
    pathlib.Path(path).write_text(new_text)
    line_summary = toolkits if len(toolkits) <= 120 else toolkits[:117] + "..."
    print(f"refresh-composio-list: updated {path} -> {line_summary}")
PYEOF
}

substitute "$REPO_AGENTS"
substitute "$WORKSPACE_AGENTS"

exit 0
