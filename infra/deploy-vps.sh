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

# ── 1b. sync identity/ → ~/.openclaw/workspace/ ────────────────
# OpenClaw reads workspace-rooted *.md files at every relevant turn
# (AGENTS.md, USER.md, SOUL.md, etc.). The workspace lives at
# ~/.openclaw/workspace/ on the box; identity/ files in this repo
# are the canonical source.
sync_identity() {
  local target="$HOME/.openclaw/workspace"
  if [ ! -d "$target" ]; then
    echo "  ${yellow}!${rst} $target missing — skipping identity sync (run openclaw onboard first?)"
    return 0
  fi
  local copied=0
  for f in identity/*.md; do
    [ -f "$f" ] || continue
    local name="$(basename "$f")"
    local dst="$target/$name"
    if [ ! -f "$dst" ] || ! cmp -s "$f" "$dst"; then
      [ -f "$dst" ] && cp "$dst" "$dst.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      cp "$f" "$dst"
      copied=$((copied+1))
    fi
  done
  # AGENTS.md is also referenced from ~/.openclaw/identity/ — keep both in sync.
  if [ -d "$HOME/.openclaw/identity" ] && [ -f identity/AGENTS.md ]; then
    if ! cmp -s identity/AGENTS.md "$HOME/.openclaw/identity/AGENTS.md"; then
      cp "$HOME/.openclaw/identity/AGENTS.md" "$HOME/.openclaw/identity/AGENTS.md.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      cp identity/AGENTS.md "$HOME/.openclaw/identity/AGENTS.md"
    fi
  fi
  echo "  synced $copied identity file(s) → $target/"
  return 0
}
step "sync identity/" sync_identity

# ── 1c. sync skills/ → ~/.openclaw/workspace/skills/<name>/ ─────
# Per-skill: copy SKILL.md and eval.yaml (if present) into the live
# workspace's skills/ tree. Skills only in our repo are NEW (created);
# skills with a name on both sides get our version (per Decision 2).
# Skills only on VPS (gbrain-shipped + custom we don't track) are
# preserved untouched.
sync_skills() {
  local target="$HOME/.openclaw/workspace/skills"
  if [ ! -d "$target" ]; then
    echo "  ${yellow}!${rst} $target missing — skipping skills sync"
    return 0
  fi
  local copied=0
  for d in skills/*/; do
    [ -f "${d}SKILL.md" ] || continue
    local name="$(basename "$d")"
    # Skip TEMPLATE-eval.yaml at top level (caught by the for-loop guard already)
    local skill_target="$target/$name"
    mkdir -p "$skill_target"
    for f in SKILL.md eval.yaml; do
      [ -f "${d}$f" ] || continue
      local dst="$skill_target/$f"
      if [ ! -f "$dst" ] || ! cmp -s "${d}$f" "$dst"; then
        [ -f "$dst" ] && cp "$dst" "$dst.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
        cp "${d}$f" "$dst"
        copied=$((copied+1))
      fi
    done
  done
  echo "  synced $copied skill file(s) → $target/"
  return 0
}
step "sync skills/" sync_skills

# ── 2. sync smoke-tests ─────────────────────────────────────────
step "sync ~/.gbrain/smoke-tests.d/" bash infra/sync-smoke-tests.sh

# ── 3. install OpenClaw community skills ────────────────────────
install_skills() {
  if ! command -v openclaw >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} openclaw not on PATH — skipping skill install"
    return 0
  fi
  # Subcommand is `openclaw skills` (plural) per `openclaw --help`.
  # Pre-2026-05 builds used `skill` (singular); we no longer support those.
  local rc=0
  for s in agent-browser humanizer; do
    if openclaw skills list 2>/dev/null | grep -q "^[* ]*$s\b"; then
      echo "  ${green}✓${rst} $s already installed"
    else
      echo "  installing $s ..."
      if openclaw skills install "$s" 2>&1 | tail -3; then
        echo "  ${green}✓${rst} $s installed"
      else
        echo "  ${yellow}!${rst} $s install non-zero — may already be present under a different namespace; non-fatal"
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

# ── 5. restart openclaw-gateway so AGENTS.md / SKILL.md changes take effect ─
restart_gateway() {
  if ! systemctl --user is-active openclaw-gateway.service >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} openclaw-gateway not active — skipping restart"
    return 0
  fi
  systemctl --user restart openclaw-gateway.service
  sleep 4
  if systemctl --user is-active openclaw-gateway.service >/dev/null 2>&1; then
    echo "  gateway restarted; active"
    return 0
  fi
  echo "  ${red}gateway failed to come back up${rst}"
  systemctl --user status openclaw-gateway.service --no-pager | head -10
  return 1
}
step "restart openclaw-gateway" restart_gateway

# ── 6. final verification: gbrain smoke-test ────────────────────
verify_smoke() {
  local gb bun_bin path_with_bun
  gb="$(command -v gbrain 2>/dev/null || true)"
  if [ -z "$gb" ]; then
    for cand in "$HOME/.bun/bin/gbrain" /usr/local/bin/gbrain /opt/gbrain/bin/gbrain; do
      [ -x "$cand" ] && gb="$cand" && break
    done
  fi
  if [ -z "$gb" ]; then
    echo "  ${yellow}!${rst} gbrain not on PATH or ~/.bun/bin — skipping smoke-test loop"
    return 0
  fi
  # gbrain is a bun script; PATH must include the bun binary.
  bun_bin="$(command -v bun 2>/dev/null || true)"
  [ -z "$bun_bin" ] && [ -x "$HOME/.bun/bin/bun" ] && bun_bin="$HOME/.bun/bin/bun"
  path_with_bun="$([ -n "$bun_bin" ] && dirname "$bun_bin" || dirname "$gb"):$PATH"
  PATH="$path_with_bun" "$gb" smoke-test
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
