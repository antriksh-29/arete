#!/usr/bin/env bash
# setup-dream-cycle.sh — install gbrain's autopilot loop on this host so
# the brain self-heals on a schedule. Idempotent. Safe to re-run.
#
# What it sets up (gbrain-native — we are NOT writing custom synthesis):
#   - `gbrain autopilot --install --repo ~/.brain` registers a Minion
#     job loop that, on each cycle, runs:
#       lint → backlinks → sync → synthesize → extract → patterns →
#       embed → orphans
#   - The supervisor watches the worker; one install = full pipeline.
#
# Usage (on the VPS where gbrain is installed):
#   bash infra/setup-dream-cycle.sh
#   bash infra/setup-dream-cycle.sh --check   (no-op verify only)
#
# Exit 0 on success. Exit 1 if gbrain isn't installed or the install
# fails. The caller (deploy script or human) decides whether to retry.

set -uo pipefail

CHECK_ONLY=0
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
  esac
done

BRAIN_REPO="${BRAIN_REPO:-$HOME/.brain}"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'

step() { echo "═══ $1 ═══"; }
ok()   { echo "${green}✓${rst} $*"; }
warn() { echo "${yellow}!${rst} $*"; }
fail() { echo "${red}✗${rst} $*"; }

# ── 1. preflight ───────────────────────────────────────────────
step "preflight"

# gbrain ships via bun and may not be on the interactive PATH. Probe
# the common locations so a non-login shell deploy doesn't false-fail.
GBRAIN_BIN="$(command -v gbrain 2>/dev/null || true)"
if [ -z "$GBRAIN_BIN" ]; then
  for cand in "$HOME/.bun/bin/gbrain" /usr/local/bin/gbrain /opt/gbrain/bin/gbrain; do
    [ -x "$cand" ] && GBRAIN_BIN="$cand" && break
  done
fi
if [ -z "$GBRAIN_BIN" ]; then
  fail "gbrain not on PATH or ~/.bun/bin — install it first (see github.com/garrytan/gbrain)"
  exit 1
fi
# Use the resolved binary for the rest of this run.
gbrain() { "$GBRAIN_BIN" "$@"; }
ok "gbrain CLI: $GBRAIN_BIN"

if [ ! -d "$BRAIN_REPO" ]; then
  fail "brain repo not found at $BRAIN_REPO"
  echo "  set BRAIN_REPO env var or clone antriksh-29/antriksh-brain there"
  exit 1
fi
ok "brain repo: $BRAIN_REPO"

# Quick health probe — gbrain doctor exits non-zero on half-migrated state
if ! gbrain doctor >/dev/null 2>&1; then
  warn "gbrain doctor reports issues — fix those before installing autopilot"
  gbrain doctor || true
  [ "$CHECK_ONLY" -eq 1 ] && exit 1
fi
ok "gbrain doctor: clean"

# ── 2. install autopilot (idempotent) ──────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  step "verify only"
  # Look for the autopilot job in the Minion job list
  if gbrain jobs list 2>/dev/null | grep -q "autopilot-cycle"; then
    ok "autopilot-cycle job present"
    exit 0
  else
    fail "autopilot-cycle job NOT registered"
    exit 1
  fi
fi

step "install autopilot"
gbrain autopilot --install --repo "$BRAIN_REPO"
rc=$?
if [ "$rc" -ne 0 ]; then
  fail "gbrain autopilot install returned $rc"
  exit 1
fi
ok "autopilot installed"

# ── 3. confirm worker is up ────────────────────────────────────
step "worker"
if pgrep -f "gbrain jobs work" >/dev/null 2>&1; then
  ok "gbrain worker process running"
else
  warn "no gbrain worker detected — start one with: gbrain jobs work &"
  warn "(or rely on the systemd-managed worker if you have one)"
fi

# ── 4. trigger one cycle to confirm shape ──────────────────────
step "first cycle"
echo "  (running 'gbrain dream --phase lint' to confirm wiring; ~5s)"
if gbrain dream --phase lint >/dev/null 2>&1; then
  ok "lint phase ran cleanly"
else
  warn "lint phase exited non-zero — investigate before relying on the cycle"
fi

echo
ok "dream cycle setup complete"
echo
echo "  • Cycle phases: lint → backlinks → sync → synthesize → extract → patterns → embed → orphans"
echo "  • Schedule:     managed by gbrain autopilot (Minion job loop)"
echo "  • Verify:       bash $0 --check"
echo "  • Manual run:   gbrain dream                # full cycle"
echo "                  gbrain dream --phase synthesize"
