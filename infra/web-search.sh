#!/bin/bash
# Web search via browser-harness against DuckDuckGo HTML.
# Replaces the prior OpenAI web_search_preview implementation (decision
# 2026-05-16: keep web access on infra we own, not on the model
# provider's tool surface). The python script does the real work; this
# wrapper exists so callers that expected the .sh entrypoint keep
# working and the audit gate's smoke target stays the same shape.
#
# Usage:  web-search.sh "<query>"
# Smoke:  web-search.sh --smoke

set -e

# --smoke: cheap health probe for skill eval. Does not drive the browser.
if [ "${1:-}" = "--smoke" ]; then
  exec python3 "$(dirname "$0")/web-search.py" --smoke
fi

exec python3 "$(dirname "$0")/web-search.py" "$@"
