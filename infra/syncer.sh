#!/usr/bin/env bash
# /opt/arete/infra/syncer.sh — batches per-Telegram-turn changes in $STATE_HOME
# into a single commit + push to the per-tenant GitHub repo.
#
# Design (v1 — simplest correct):
#   - Poll every POLL_INTERVAL_SECONDS (default 5s).
#   - If `git status` shows changes AND no further changes in DEBOUNCE_SECONDS (default 8s),
#     stage everything except excluded paths, build a readable commit message, commit + push.
#   - Excludes: transcripts/raw/ (volume-only, never to git), creds/ (vault-only).
#   - Pushes use the GITHUB_PAT injected via the repo's authenticated remote URL.
#
# v2 (later): hook into OpenClaw's `--on-turn-end` event for instant flushes instead of polling.

set -euo pipefail

: "${STATE_HOME:?STATE_HOME required}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-8}"

cd "${STATE_HOME}"

# Apply gitignore at runtime if the template didn't ship one (idempotent).
if [ ! -f .gitignore ]; then
  cat > .gitignore <<'EOF'
transcripts/raw/
creds/
*.tmp
*.swp
.DS_Store
EOF
fi

# Returns the timestamp of the most recent modification in the working tree
# (anything tracked or untracked, excluding gitignored). Used for debounce.
latest_mtime() {
  # %T@ = epoch seconds (with fractional). git ls-files for tracked, plus untracked-not-ignored.
  {
    git ls-files
    git ls-files --others --exclude-standard
  } | xargs -I{} stat -c '%Y' "{}" 2>/dev/null | sort -nr | head -n1
}

build_commit_message() {
  # Summarize changed paths into a one-line + multi-line message.
  local changed
  changed="$(git status --porcelain | awk '{print $2}')"
  local first_line
  first_line="$(echo "${changed}" | head -n3 | tr '\n' ',' | sed 's/,$//' | sed 's/,/, /g')"
  local count
  count="$(echo "${changed}" | wc -l | tr -d ' ')"
  if [ "${count}" -gt 3 ]; then
    echo "sync: ${first_line} (+$((count - 3)) more)"
  else
    echo "sync: ${first_line}"
  fi
  echo ""
  echo "Files changed:"
  echo "${changed}" | sed 's/^/  /'
}

echo "[syncer] watching ${STATE_HOME} (poll=${POLL_INTERVAL_SECONDS}s, debounce=${DEBOUNCE_SECONDS}s)"

while true; do
  sleep "${POLL_INTERVAL_SECONDS}"

  # Anything to commit?
  if [ -z "$(git status --porcelain)" ]; then
    continue
  fi

  # Debounce: wait until the working tree has been quiet for DEBOUNCE_SECONDS.
  last_mtime="$(latest_mtime || echo 0)"
  now="$(date +%s)"
  if [ -n "${last_mtime}" ] && [ "$((now - last_mtime))" -lt "${DEBOUNCE_SECONDS}" ]; then
    continue
  fi

  # Stage everything (gitignore filters out transcripts/raw/ + creds/).
  git add -A

  # Re-check after staging in case the only change was an ignored file.
  if git diff --cached --quiet; then
    continue
  fi

  msg="$(build_commit_message)"
  git commit -m "${msg}" --no-verify > /dev/null

  # Push. On failure (network blip, rate limit), log and retry next loop.
  if ! git push origin HEAD 2>&1; then
    echo "[syncer] push failed, will retry next tick" >&2
    continue
  fi

  echo "[syncer] pushed: $(echo "${msg}" | head -n1)"
done
