#!/usr/bin/env bash
# sync-smoke-tests.sh — copy this repo's infra/smoke-tests/*.sh into
# ~/.gbrain/smoke-tests.d/ so they get picked up by `gbrain smoke-test`.
#
# Idempotent. Diffs source vs target before copying — only updates files
# that actually changed.
#
# Usage:
#   bash infra/sync-smoke-tests.sh         # sync (copy)
#   bash infra/sync-smoke-tests.sh --check  # report-only, exit non-zero on drift

set -uo pipefail
cd "$(dirname "$0")/.."

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

SRC_DIR="infra/smoke-tests"
DST_DIR="$HOME/.gbrain/smoke-tests.d"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'

mkdir -p "$DST_DIR"

drift=0
copied=0
for src in "$SRC_DIR"/50-skill-*.sh; do
  [ -f "$src" ] || continue
  name="$(basename "$src")"
  dst="$DST_DIR/$name"
  if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
    drift=$((drift+1))
    if [ "$CHECK_ONLY" -eq 1 ]; then
      echo "${yellow}!${rst} drift: $name"
    else
      cp "$src" "$dst"
      chmod +x "$dst"
      echo "${green}✓${rst} synced $name"
      copied=$((copied+1))
    fi
  fi
done

# Also sync the shared lib (no 50- prefix → not run by smoke-test loader)
if [ -f "$SRC_DIR/_lib.sh" ]; then
  if [ ! -f "$DST_DIR/_lib.sh" ] || ! cmp -s "$SRC_DIR/_lib.sh" "$DST_DIR/_lib.sh"; then
    if [ "$CHECK_ONLY" -eq 1 ]; then
      echo "${yellow}!${rst} drift: _lib.sh"
      drift=$((drift+1))
    else
      cp "$SRC_DIR/_lib.sh" "$DST_DIR/_lib.sh"
      echo "${green}✓${rst} synced _lib.sh"
    fi
  fi
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ "$drift" -gt 0 ]; then
    echo
    echo "${red}=== $drift file(s) need syncing ===${rst}"
    echo "Run: bash infra/sync-smoke-tests.sh"
    exit 1
  fi
  echo "${green}=== smoke-tests in sync ===${rst}"
  exit 0
fi

echo
echo "${green}=== synced $copied file(s) into $DST_DIR ===${rst}"
echo
echo "Verify with:    gbrain smoke-test"
echo "or directly:    bash $DST_DIR/50-skill-<name>.sh"
