#!/usr/bin/env bash
# deploy-vps.sh — orchestrator for "deploy this repo to the bot VPS."
# Wraps the existing pieces so a deploy is one command:
#
#   1. Sync infra/ scripts into ~/bin/                (existing convention)
#   2. Sync infra/smoke-tests/ into ~/.gbrain/smoke-tests.d/
#   3. Install OpenClaw community skills (agent-browser, humanizer)
#   4. Confirm gbrain autopilot is installed (dream cycle)
#   5. Run gbrain smoke-test for full verification
#
# Designed to run ON the VPS (not locally). Locally, use --check to
# confirm all the constituent scripts exist and are executable.
#
# Usage:
#   bash infra/deploy-vps.sh             # full deploy
#   bash infra/deploy-vps.sh --check     # dry-run; verify all pieces exist
#   bash infra/deploy-vps.sh --skip-skills   # skip openclaw skill install
#   bash infra/deploy-vps.sh --skip-dream    # skip dream-cycle autopilot install

set -uo pipefail
cd "$(dirname "$0")/.."

CHECK_ONLY=0
SKIP_SKILLS=0
SKIP_DREAM=0
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --skip-skills) SKIP_SKILLS=1 ;;
    --skip-dream) SKIP_DREAM=1 ;;
  esac
done

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'
fails=0
ran=0

step() {
  local name="$1"; shift
  echo
  echo "═══ $name ═══"
  ran=$((ran+1))
  if "$@"; then
    echo "${green}✓${rst} $name"
  else
    echo "${red}✗${rst} $name"
    fails=$((fails+1))
  fi
}

skip() { echo; echo "═══ skip: $1 ═══"; }

# ── --check: verify constituents exist ──────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "═══ deploy-vps --check ═══"
  required=(
    "infra/sync-smoke-tests.sh"
    "infra/setup-dream-cycle.sh"
    "infra/smoke-tests/_lib.sh"
    "eval/run-all-evals.sh"
  )
  missing=0
  for f in "${required[@]}"; do
    if [ -f "$f" ]; then echo "${green}✓${rst} $f"; else echo "${red}✗${rst} missing: $f"; missing=$((missing+1)); fi
  done
  echo
  if [ "$missing" -gt 0 ]; then
    echo "${red}=== $missing required file(s) missing ===${rst}"
    exit 1
  fi
  echo "${green}=== all constituents present ===${rst}"
  exit 0
fi

# ── 1. sync infra/ scripts into ~/bin/ ──────────────────────────
sync_bin() {
  mkdir -p "$HOME/bin"
  local copied=0
  for f in infra/*.py infra/*.sh; do
    [ -f "$f" ] || continue
    # Skip orchestrators and library modules — only deploy callable scripts.
    case "$(basename "$f")" in
      deploy-vps.sh|install-pre-push-hook.sh|sync-smoke-tests.sh|setup-dream-cycle.sh) continue ;;
    esac
    local dst="$HOME/bin/$(basename "$f")"
    if [ ! -f "$dst" ] || ! cmp -s "$f" "$dst"; then
      cp "$f" "$dst"
      chmod +x "$dst"
      copied=$((copied+1))
    fi
  done
  echo "  synced $copied script(s) → $HOME/bin/"
  return 0
}
step "sync ~/bin/" sync_bin

# ── 2. sync smoke-tests ─────────────────────────────────────────
step "sync ~/.gbrain/smoke-tests.d/" bash infra/sync-smoke-tests.sh

# ── 3. install OpenClaw community skills ────────────────────────
install_skills() {
  if ! command -v openclaw >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} openclaw not on PATH — skipping skill install"
    return 0
  fi
  local rc=0
  for s in agent-browser humanizer; do
    if openclaw skill list 2>/dev/null | grep -q "^$s"; then
      echo "  ${green}✓${rst} $s already installed"
    else
      echo "  installing $s ..."
      if openclaw skill install "$s"; then
        echo "  ${green}✓${rst} $s installed"
      else
        echo "  ${red}✗${rst} $s install failed"
        rc=1
      fi
    fi
  done
  return $rc
}
if [ "$SKIP_SKILLS" -eq 1 ]; then skip "openclaw skills"; else step "openclaw skills" install_skills; fi

# ── 4. dream-cycle autopilot ────────────────────────────────────
if [ "$SKIP_DREAM" -eq 1 ]; then
  skip "dream-cycle autopilot"
else
  # If already installed, --check exits 0; only run full install if not.
  if bash infra/setup-dream-cycle.sh --check >/dev/null 2>&1; then
    step "dream-cycle (already installed)" true
  else
    step "dream-cycle install" bash infra/setup-dream-cycle.sh
  fi
fi

# ── 5. final verification: gbrain smoke-test ────────────────────
verify_smoke() {
  if ! command -v gbrain >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} gbrain not on PATH — skipping smoke-test loop"
    return 0
  fi
  gbrain smoke-test
}
step "gbrain smoke-test" verify_smoke

# ── verdict ─────────────────────────────────────────────────────
echo
if [ "$fails" -gt 0 ]; then
  echo "${red}=== $fails of $ran step(s) FAILED ===${rst}"
  echo "  re-run after addressing the failures, or use --skip-* to bypass a known-broken step."
  exit 1
fi
echo "${green}=== deploy complete · $ran step(s) green ===${rst}"
echo
echo "Next:"
echo "  1. Confirm Telegram polling is up:  systemctl --user status openclaw-gateway"
echo "  2. Smoke-check a routing skill:     ~/bin/water-log.sh --smoke"
echo "  3. Tail logs:                       journalctl --user -u openclaw-gateway -f"
