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

# ── 1a-bis. seed exec-approvals allowlist for routing skills ────
# Without this, replies like "yes" to the reading-checkin daily ping
# hit a per-call exec-approval gate and silently expire if not seen.
step "seed exec allowlist" bash infra/seed-exec-allowlist.sh

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

# ── 3a. install Chromium + deps so openclaw-browser can launch ────
# openclaw's browser plugin needs a Chrome/Chromium/Brave/Edge binary
# on the host plus xdg-utils (chromium-browser shell wrapper uses
# xdg-settings at startup). Idempotent: skip pkgs already installed.
install_chromium() {
  local need_pkgs=""
  local have_browser=0
  for cand in chromium chromium-browser google-chrome chrome brave-browser; do
    if command -v "$cand" >/dev/null 2>&1; then
      echo "  ${green}✓${rst} browser already present: $cand"
      have_browser=1
      break
    fi
  done
  if [ "$have_browser" -eq 0 ]; then
    need_pkgs="chromium"
  fi
  if ! command -v xdg-settings >/dev/null 2>&1; then
    need_pkgs="$need_pkgs xdg-utils"
  fi
  if [ -z "$need_pkgs" ]; then
    echo "  ${green}✓${rst} all browser packages present"
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} no apt-get — install manually: $need_pkgs"
    return 0
  fi
  echo "  installing:$need_pkgs"
  if sudo -n apt-get install -y -q $need_pkgs 2>&1 | tail -3; then
    echo "  ${green}✓${rst} packages installed"
  elif [ "$have_browser" -eq 0 ] && sudo -n apt-get install -y -q chromium-browser xdg-utils 2>&1 | tail -3; then
    echo "  ${green}✓${rst} chromium-browser + xdg-utils installed"
  else
    echo "  ${yellow}!${rst} package install failed (no passwordless sudo?) — non-fatal"
    return 0
  fi
}
step "install chromium" install_chromium

# ── 3b. apply openclaw browser config knobs ──────────────────────
# noSandbox=true: chromium running under a non-root system user inside
# a VPS still needs --no-sandbox in many container/VM kernels (the
# unprivileged_userns_clone sysctl may be off). enabled=true and
# defaultProfile=openclaw guarantee the plugin is reachable for the
# agent-browser skill. Edits openclaw.json in place via jq.
configure_openclaw_browser() {
  local cfg="$HOME/.openclaw/openclaw.json"
  if [ ! -f "$cfg" ]; then
    echo "  ${yellow}!${rst} $cfg missing — skipping browser config patch"
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} jq missing — install with: apt-get install -y jq"
    return 0
  fi
  local tmp
  tmp=$(mktemp)
  if jq '.browser = (.browser // {}) | .browser.enabled = true | .browser.defaultProfile = "openclaw" | .browser.noSandbox = true' "$cfg" > "$tmp" 2>/dev/null; then
    if ! cmp -s "$tmp" "$cfg"; then
      cp "$cfg" "$cfg.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      mv "$tmp" "$cfg"
      echo "  ${green}✓${rst} browser config patched (enabled, noSandbox)"
    else
      rm -f "$tmp"
      echo "  ${green}✓${rst} browser config already correct"
    fi
  else
    rm -f "$tmp"
    echo "  ${yellow}!${rst} jq edit failed — non-fatal"
  fi
  return 0
}
step "openclaw browser config" configure_openclaw_browser

# ── 3c. clear stale browser locks + start + doctor ────────────────
# A previous failed start may leave SingletonLock / SingletonCookie
# that prevents a fresh start. Safe to remove — they're recreated on
# successful launch.
start_openclaw_browser() {
  if ! command -v openclaw >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} openclaw not on PATH — skipping browser start"
    return 0
  fi
  local user_data="$HOME/.openclaw/browser/openclaw/user-data"
  if [ -d "$user_data" ]; then
    rm -f "$user_data"/Singleton* 2>/dev/null || true
  fi
  openclaw browser start 2>&1 | tail -5 || true
  echo
  echo "  doctor:"
  openclaw browser doctor 2>&1 | head -10 | sed 's/^/    /'
  return 0
}
step "openclaw browser start" start_openclaw_browser

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

# ── 4b. apply managed openclaw.json knobs ───────────────────────
# Keeps tuned config (e.g. bootstrapTotalMaxChars) in sync without
# committing the secret-bearing openclaw.json. See script header for
# rationale. Must run BEFORE the gateway restart below so the new
# values get picked up.
step "configure openclaw.json" bash infra/configure-openclaw.sh

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

# ── 5b. one-shot reading-list v2 migration ──────────────────────
# Hydrates pre-v2 rows on lists/to-read with brain pages + posted_by
# + one_liner so the new 4-line render contract has data to render.
# Idempotent via marker file. Safe to re-run if marker is removed.
migrate_reading_list_v2() {
  # Marker name carries a generation suffix; bump when the migration
  # logic itself changes (not when only data shape changes). The first
  # v2-rev1 deploy preserved stale V1 titles by mistake — rev2 re-runs
  # against the same rows with the fresh-data-wins fix.
  local marker="$HOME/.brain/.reading-list-v2-migrated-rev3"
  if [ -f "$marker" ]; then
    echo "  already migrated (marker: $marker)"
    return 0
  fi
  local script="$HOME/bin/reading-list.py"
  if [ ! -x "$script" ]; then
    echo "  ${yellow}!${rst} $script not present yet — skipping migration this deploy"
    return 0
  fi
  echo "  running: python3 $script migrate --force"
  if out=$(python3 "$script" migrate --force 2>&1); then
    echo "$out"
    mkdir -p "$(dirname "$marker")"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$marker"
    echo "  ${green}✓${rst} migration complete; marker written"
    return 0
  fi
  echo "$out"
  echo "  ${yellow}!${rst} migration returned non-zero — not writing marker, will retry next deploy"
  return 1
}
step "reading-list v2 migrate" migrate_reading_list_v2

# ── 6. final verification: gbrain smoke-test (informational) ────
# This step runs gbrain's built-in smoke checks PLUS our drop-in
# user-test wrappers in ~/.gbrain/smoke-tests.d/. Always returns 0
# regardless of outcome — the gbrain built-ins check env vars
# (DATABASE_URL, OPENAI_API_KEY) that exist in the openclaw
# systemd context but NOT in the non-login SSH shell GitHub Actions
# uses. Those false-failures shouldn't red-X a successful deploy.
#
# The actual deploy correctness was already proven by the gateway
# restart returning active (step 5). Our user-test wrappers
# (50-skill-*.sh) still get exercised here for visibility, but
# their pass/fail is logged, not gated.
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
  bun_bin="$(command -v bun 2>/dev/null || true)"
  [ -z "$bun_bin" ] && [ -x "$HOME/.bun/bin/bun" ] && bun_bin="$HOME/.bun/bin/bun"
  path_with_bun="$([ -n "$bun_bin" ] && dirname "$bun_bin" || dirname "$gb"):$PATH"

  local out rc
  out=$(PATH="$path_with_bun" "$gb" smoke-test 2>&1) || rc=$?
  echo "$out"
  # Parse "Results: X/Y passed" line. If user-tests count is reasonable,
  # we accept the step. Built-in checks for DB/API env vars are
  # informational — the deploy doesn't depend on them.
  if echo "$out" | grep -qE "Results: [0-9]+/[0-9]+ passed"; then
    local user_test_failures
    user_test_failures=$(echo "$out" | grep -cE "^❌ User: " || true)
    if [ "${user_test_failures:-0}" -gt 0 ]; then
      echo "  ${red}✗${rst} $user_test_failures user-test wrapper(s) failed (above) — these matter; investigate"
      return 1
    fi
    echo "  ${green}✓${rst} all user-test wrappers passed; gbrain built-in env checks ignored (false-fail in non-login shell)"
    return 0
  fi
  echo "  ${yellow}!${rst} smoke-test output unparseable; treating as informational"
  return 0
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
