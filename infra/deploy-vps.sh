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

# ── 1c-bis. sync openclaw/skills/ → ~/.openclaw/workspace/skills/<name>/ ─
# Same logic as sync_skills, but sourced from openclaw/skills/ (skills
# imported from the upstream OpenClaw catalog at openclaw/openclaw, kept
# separate from our own skills/ tree to preserve provenance — see the
# 2026-05-16 openclaw 2026.5.12 integration commit). Our gstack-format
# SKILL.md files here override the bundled openclaw-npm definitions for
# any skill name that matches.
sync_openclaw_skills() {
  local target="$HOME/.openclaw/workspace/skills"
  if [ ! -d "$target" ]; then
    echo "  ${yellow}!${rst} $target missing — skipping openclaw-skills sync"
    return 0
  fi
  if [ ! -d openclaw/skills ]; then
    echo "  openclaw/skills/ not present in repo — nothing to sync"
    return 0
  fi
  local copied=0
  for d in openclaw/skills/*/; do
    [ -f "${d}SKILL.md" ] || continue
    local name="$(basename "$d")"
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
  echo "  synced $copied openclaw-skill file(s) → $target/"
  return 0
}
step "sync openclaw/skills/" sync_openclaw_skills

# ── 1d. sync user-skills/ → ~/.openclaw/workspace/skills/<name>/ ─
# user-skills/ is the Telegram-authored skill overlay. It wins on every
# name conflict against skills/ and openclaw/skills/ — see
# identity/AGENTS.md → ## User-skills overlay. To enforce this on disk,
# sync_user_skills() runs LAST (last-sync-wins). Also handles zombie
# deletion: any directory under workspace/skills/ whose name was
# previously synced by us but no longer exists in user-skills/ at HEAD
# gets moved aside.
sync_user_skills() {
  local target="$HOME/.openclaw/workspace/skills"
  local tracker="$HOME/.openclaw/.user-skills-managed.list"
  if [ ! -d "$target" ]; then
    echo "  ${yellow}!${rst} $target missing — skipping user-skills sync"
    return 0
  fi
  if [ ! -d user-skills ]; then
    echo "  user-skills/ not present in repo — nothing to sync"
    user_skills_zombie_delete "$target" "$tracker" ""
    return 0
  fi
  local copied=0
  local current_names=""
  for d in user-skills/*/; do
    [ -f "${d}SKILL.md" ] || continue
    local name="$(basename "$d")"
    current_names="$current_names $name"
    local skill_target="$target/$name"
    mkdir -p "$skill_target"
    # File allowlist mirrors safety-scan.ts: SKILL.md, eval.yaml, .usage.json only.
    for f in SKILL.md eval.yaml .usage.json; do
      [ -f "${d}$f" ] || continue
      local dst="$skill_target/$f"
      if [ ! -f "$dst" ] || ! cmp -s "${d}$f" "$dst"; then
        [ -f "$dst" ] && cp "$dst" "$dst.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
        cp "${d}$f" "$dst"
        copied=$((copied+1))
      fi
    done
    # Reject any other file types in the source — keeps the workspace
    # honest even if someone hand-edits user-skills/ outside the
    # safety-scan path.
    local stray
    stray=$(find "$d" -mindepth 1 -maxdepth 1 -type f ! \( -name SKILL.md -o -name eval.yaml -o -name .usage.json \) 2>/dev/null | head -3)
    if [ -n "$stray" ]; then
      echo "  ${yellow}!${rst} user-skills/$name/ has disallowed file types — not syncing those:"
      echo "$stray" | sed 's/^/    /'
    fi
  done
  echo "  synced $copied user-skill file(s) → $target/"
  user_skills_zombie_delete "$target" "$tracker" "$current_names"
  printf '%s\n' $current_names > "$tracker"
  return 0
}

user_skills_zombie_delete() {
  local target="$1"
  local tracker="$2"
  local current_names="$3"
  [ -f "$tracker" ] || return 0
  local prev_names removed=0
  prev_names=$(cat "$tracker" 2>/dev/null | tr '\n' ' ')
  for n in $prev_names; do
    [ -z "$n" ] && continue
    if ! echo " $current_names " | grep -q " $n "; then
      if [ -d "$target/$n" ]; then
        local backup="$target/$n.zombie.$(date +%Y%m%d-%H%M%S)"
        mv "$target/$n" "$backup" 2>/dev/null && {
          echo "  ${yellow}-${rst} removed zombie user-skill $n (backup: $backup)"
          removed=$((removed+1))
        }
      fi
    fi
  done
  [ "$removed" -gt 0 ] && echo "  removed $removed zombie user-skill(s)"
  return 0
}
step "sync user-skills/" sync_user_skills

# ── 1e. pre-flight: verify openclaw-gateway supports graceful reload ─
# If the unit has no ExecReload, deploys full-restart and kill in-flight
# Telegram turns — surface that fact loudly so we know.
check_gateway_reload() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "  ${yellow}!${rst} systemctl not available — skipping reload-capability check"
    return 0
  fi
  local exec_reload
  exec_reload=$(systemctl --user show openclaw-gateway --property=ExecReload 2>/dev/null | cut -d= -f2-)
  if [ -z "$exec_reload" ]; then
    echo "  ${yellow}!${rst} openclaw-gateway has no ExecReload — deploys will full-restart (kills in-flight Telegram turns)"
  else
    echo "  ${green}✓${rst} openclaw-gateway supports graceful reload"
  fi
  return 0
}
step "check gateway reload capability" check_gateway_reload

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

# ── 3a. install Google Chrome (non-snap) + xdg-utils ──────────────
# browser-harness needs a real Chrome on the host (no snap confinement
# — snap chromium can't write to arbitrary user-data dirs).
# Idempotent: skip if google-chrome is already present.
install_chrome() {
  if command -v google-chrome >/dev/null 2>&1; then
    echo "  ${green}✓${rst} google-chrome present"
  elif command -v apt-get >/dev/null 2>&1; then
    if [ ! -f /etc/apt/sources.list.d/google-chrome.list ]; then
      sudo -n bash -c '
        set -e
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
          | gpg --dearmor -o /etc/apt/trusted.gpg.d/google-chrome.gpg
        echo "deb [arch=amd64 signed-by=/etc/apt/trusted.gpg.d/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
          > /etc/apt/sources.list.d/google-chrome.list
        apt-get update -q
      ' 2>&1 | tail -3 || true
    fi
    if sudo -n apt-get install -y -q google-chrome-stable 2>&1 | tail -3; then
      echo "  ${green}✓${rst} google-chrome-stable installed"
    else
      echo "  ${yellow}!${rst} google-chrome install failed — non-fatal"
    fi
  else
    echo "  ${yellow}!${rst} no apt-get — install google-chrome manually"
  fi
  # xdg-utils is referenced by some chrome features; harmless if absent
  if ! command -v xdg-settings >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    sudo -n apt-get install -y -q xdg-utils 2>&1 | tail -1 || true
  fi
  return 0
}
step "install google-chrome" install_chrome

# ── 3b. install uv + browser-harness ──────────────────────────────
# uv is the Python tool installer browser-harness uses. We clone the
# repo to ~/.browser-harness (separate from chrome-data) and
# `uv tool install -e .` so the `browser-harness` CLI lands on PATH.
BH_REPO_DIR="$HOME/.browser-harness/repo"
install_browser_harness() {
  # Install uv if missing
  if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "  installing uv ..."
    curl -fsSL https://astral.sh/uv/install.sh | sh 2>&1 | tail -3 || true
  fi
  local uv_bin
  uv_bin="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
  if [ ! -x "$uv_bin" ]; then
    echo "  ${yellow}!${rst} uv install failed — non-fatal"
    return 0
  fi
  # Clone or update the repo
  if [ ! -d "$BH_REPO_DIR/.git" ]; then
    mkdir -p "$(dirname "$BH_REPO_DIR")"
    git clone --depth 1 https://github.com/browser-use/browser-harness "$BH_REPO_DIR" 2>&1 | tail -3 || true
  else
    ( cd "$BH_REPO_DIR" && git fetch origin --depth 1 main 2>&1 | tail -1 && git reset --hard origin/main 2>&1 | tail -1 ) || true
  fi
  if [ ! -f "$BH_REPO_DIR/pyproject.toml" ]; then
    echo "  ${yellow}!${rst} browser-harness clone incomplete — non-fatal"
    return 0
  fi
  # Editable tool install
  ( cd "$BH_REPO_DIR" && "$uv_bin" tool install -e . 2>&1 | tail -5 ) || true
  if [ -x "$HOME/.local/bin/browser-harness" ] || command -v browser-harness >/dev/null 2>&1; then
    echo "  ${green}✓${rst} browser-harness CLI installed: $(command -v browser-harness 2>/dev/null || echo "$HOME/.local/bin/browser-harness")"
  else
    echo "  ${yellow}!${rst} browser-harness CLI not on PATH — non-fatal"
  fi
  return 0
}
step "install browser-harness" install_browser_harness

# ── 3c. install bh-chrome systemd user unit + start ──────────────
# Persistent headless Chrome on CDP port 9222. browser-harness attaches
# to this Chrome for every command. user-data dir persists cookies +
# session state so once a domain is logged in, it stays logged in
# across restarts.
install_bh_chrome_unit() {
  local unit_src="infra/bh-chrome.service"
  local unit_dst="$HOME/.config/systemd/user/bh-chrome.service"
  if [ ! -f "$unit_src" ]; then
    echo "  ${yellow}!${rst} $unit_src missing — non-fatal"
    return 0
  fi
  mkdir -p "$(dirname "$unit_dst")" "$HOME/.browser-harness/chrome-data"
  if [ ! -f "$unit_dst" ] || ! cmp -s "$unit_src" "$unit_dst"; then
    cp "$unit_src" "$unit_dst"
    systemctl --user daemon-reload 2>&1 | tail -1 || true
    echo "  ${green}✓${rst} bh-chrome.service synced"
  else
    echo "  ${green}✓${rst} bh-chrome.service up to date"
  fi
  systemctl --user enable bh-chrome.service 2>&1 | tail -1 || true
  if systemctl --user is-active bh-chrome.service >/dev/null 2>&1; then
    # Restart to pick up any unit changes
    systemctl --user restart bh-chrome.service
  else
    systemctl --user start bh-chrome.service
  fi
  sleep 3
  if systemctl --user is-active bh-chrome.service >/dev/null 2>&1; then
    echo "  ${green}✓${rst} bh-chrome.service active"
  else
    echo "  ${red}✗${rst} bh-chrome.service failed to start"
    systemctl --user status bh-chrome.service --no-pager | head -15 | sed 's/^/    /'
    return 1
  fi
  # Verify CDP endpoint is reachable
  if curl -sf --max-time 5 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    echo "  ${green}✓${rst} CDP reachable on 127.0.0.1:9222"
  else
    echo "  ${yellow}!${rst} CDP not yet reachable — Chrome may still be starting"
  fi
  return 0
}
step "install bh-chrome unit" install_bh_chrome_unit

# ── 3c-bis. install daily-tasks-review timer + service ─────────
# Fires at 22:00 IST daily, runs daily-tasks-review-trigger.sh which
# sends the user the numbered status-prompt for the day's tasks.
# Before May 12, these units lived only on the VPS (not committed),
# so when the project was restructured the bot lost awareness of the
# cron and the user lost a week of evening reviews. Versioning them
# now means redeploy = guaranteed cron continuity.
install_dtr_unit() {
  local changed=0
  for f in daily-tasks-review.timer daily-tasks-review.service; do
    local src="infra/$f"
    local dst="$HOME/.config/systemd/user/$f"
    if [ ! -f "$src" ]; then
      echo "  ${yellow}!${rst} $src missing — non-fatal"
      continue
    fi
    mkdir -p "$(dirname "$dst")"
    if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
      cp "$src" "$dst"
      changed=1
      echo "  ${green}✓${rst} $f synced"
    fi
  done
  if [ "$changed" -eq 1 ]; then
    systemctl --user daemon-reload 2>&1 | tail -1 || true
  fi
  systemctl --user enable daily-tasks-review.timer 2>&1 | tail -1 || true
  if systemctl --user is-active daily-tasks-review.timer >/dev/null 2>&1; then
    systemctl --user restart daily-tasks-review.timer
  else
    systemctl --user start daily-tasks-review.timer
  fi
  if systemctl --user is-active daily-tasks-review.timer >/dev/null 2>&1; then
    echo "  ${green}✓${rst} daily-tasks-review.timer active"
  else
    echo "  ${red}✗${rst} daily-tasks-review.timer failed to start"
    return 1
  fi
  return 0
}
step "install daily-tasks-review unit" install_dtr_unit

# ── 3c-ter. install xref-drainer timer + service ──────────────────
# Drains ~/.brain/.xref-queue.jsonl every 60s. Ingestors now defer
# the per-entity gbrain link / timeline-add fan-out (~2s per entity)
# to this queue so the user-facing add path doesn't push past the
# 120s per-turn Telegram budget. Without this timer entity stubs
# would only be created when the reading-list worker kicks the drainer
# one-shot at end of its run, which is fine for new bookmarks but
# leaves migrate-path queues stranded.
install_xref_drainer_unit() {
  local changed=0
  for f in xref-drainer.service xref-drainer.timer; do
    local src="infra/$f"
    local dst="$HOME/.config/systemd/user/$f"
    if [ ! -f "$src" ]; then
      echo "  ${yellow}!${rst} $src missing — non-fatal"
      continue
    fi
    mkdir -p "$(dirname "$dst")"
    if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
      cp "$src" "$dst"
      changed=1
      echo "  ${green}✓${rst} $f synced"
    fi
  done
  if [ "$changed" -eq 1 ]; then
    systemctl --user daemon-reload 2>&1 | tail -1 || true
  fi
  systemctl --user enable xref-drainer.timer 2>&1 | tail -1 || true
  if systemctl --user is-active xref-drainer.timer >/dev/null 2>&1; then
    systemctl --user restart xref-drainer.timer
  else
    systemctl --user start xref-drainer.timer
  fi
  if systemctl --user is-active xref-drainer.timer >/dev/null 2>&1; then
    echo "  ${green}✓${rst} xref-drainer.timer active"
  else
    echo "  ${red}✗${rst} xref-drainer.timer failed to start"
    return 1
  fi
  return 0
}
step "install xref-drainer unit" install_xref_drainer_unit

# ── 3d. browser-harness doctor ────────────────────────────────────
# Sanity check the full stack. Exits 0 even on failure so a flaky
# doctor doesn't gate the rest of the deploy.
bh_doctor() {
  local bh
  bh="$(command -v browser-harness 2>/dev/null || echo "$HOME/.local/bin/browser-harness")"
  if [ ! -x "$bh" ]; then
    echo "  ${yellow}!${rst} browser-harness not on PATH — skipping doctor"
    return 0
  fi
  BU_CDP_URL="http://127.0.0.1:9222" "$bh" --doctor 2>&1 | head -20 | sed 's/^/    /' || true
  return 0
}
step "browser-harness doctor" bh_doctor

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

# ── 4c. systemd drop-in: BH_DOMAIN_SKILLS=1 for the gateway ─────
# browser-harness has a search-first learning surface: domain-skills/
# (per-site playbooks) and interaction-skills/ (UI-mechanic docs).
# Without BH_DOMAIN_SKILLS=1 in the agent's environment, domain
# skills are dead weight — the harness loads them only when this var
# is set AND the agent navigates to a matching host. The drop-in is
# small + idempotent; create it on every deploy.
install_bh_dropin() {
  local dir="$HOME/.config/systemd/user/openclaw-gateway.service.d"
  local file="$dir/browser-harness.conf"
  mkdir -p "$dir"
  cat > "$file" <<EOF
[Service]
# Surface browser-harness domain-skills automatically when the agent
# navigates to a matching site. Repo lives at ~/.browser-harness/repo/
# Documented in skills/agent-browser/SKILL.md.
Environment=BH_DOMAIN_SKILLS=1
EOF
  systemctl --user daemon-reload
  echo "  wrote $file"
}
step "browser-harness systemd drop-in" install_bh_dropin

# ── 4a. install skill-author kill-switch drop-in (T9) ─────────────
# Wires infra/skill-authoring-killswitch.sh into openclaw-gateway's
# ExecStartPre so a panic env var (SKILL_AUTHORING_PANIC=1) quarantines
# user-skills/ on next restart without needing a manual mv. See script
# header for usage.
install_killswitch_dropin() {
  local dir="$HOME/.config/systemd/user/openclaw-gateway.service.d"
  local file="$dir/skill-authoring-killswitch.conf"
  local script="$HOME/personal-assistant/infra/skill-authoring-killswitch.sh"
  mkdir -p "$dir"
  cat > "$file" <<EOF
[Service]
# Panic kill-switch: if SKILL_AUTHORING_PANIC=1, the script moves
# user-skills/ aside before the daemon starts. Otherwise it's a no-op.
# Documented in infra/skill-authoring-killswitch.sh header.
ExecStartPre=$script
# Also pass through the panic env var so the script can read it.
PassEnvironment=SKILL_AUTHORING_PANIC
EOF
  systemctl --user daemon-reload
  echo "  wrote $file"
}
step "skill-author kill-switch systemd drop-in" install_killswitch_dropin

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

# ── 5a. Telegram deploy-callback for newly-authored user-skills ─
# When a /teach or /refine commit lands and the deploy succeeds, send
# a "🎉 Live" message to the user's Telegram chat so they know their
# skill is ready. Without this, the UX dies at the most important
# moment: user confirms the draft, sees "Creating this new skill...",
# then silence forever (since the actual deploy is async via CI).
#
# Implementation: read TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID from the
# vault, detect newly-Added or Modified user-skills/<slug>/SKILL.md
# in the just-pulled commit, send one message per affected slug.
#
# Skips silently if:
# - HEAD has no parent (initial commit; nothing to diff against)
# - No user-skills/<slug>/SKILL.md changed in the commit
# - Either vault key is missing
notify_user_skill_deploys() {
  # Need a parent commit to diff against
  if ! git -C "$HOME/personal-assistant" rev-parse HEAD~1 >/dev/null 2>&1; then
    echo "  (no parent commit; skipping telegram callback)"
    return 0
  fi
  # Affected slugs: newly-added (A) or modified (M) user-skills/<slug>/SKILL.md
  local affected
  affected=$(git -C "$HOME/personal-assistant" diff --name-status HEAD~1..HEAD \
    | grep -E '^[AM][[:space:]]+user-skills/[^/]+/SKILL\.md$' \
    | awk -F/ '{print $2}' | sort -u | head -5)
  if [ -z "$affected" ]; then
    echo "  no user-skills/ changes in this deploy — skipping telegram callback"
    return 0
  fi
  local bot_token chat_id
  bot_token=$(bot-cred get TELEGRAM_BOT_TOKEN 2>/dev/null | head -1)
  chat_id=$(bot-cred get TELEGRAM_USER_ID 2>/dev/null | head -1)
  if [ -z "$bot_token" ] || [ -z "$chat_id" ]; then
    echo "  ${yellow}!${rst} telegram vault keys missing — skipping callback (skill IS deployed)"
    return 0
  fi
  local slug status msg triggers cron_line is_periodic
  while IFS= read -r slug; do
    [ -z "$slug" ] && continue
    # Determine if this was a create (A) or refine (M).
    status=$(git -C "$HOME/personal-assistant" diff --name-status HEAD~1..HEAD \
      -- "user-skills/$slug/SKILL.md" | awk '{print $1}')
    # Periodic vs manual: peek at the cron field.
    cron_line=$(grep -E '^cron:' "$HOME/personal-assistant/user-skills/$slug/SKILL.md" 2>/dev/null | head -1)
    is_periodic=$(echo "$cron_line" | grep -qE '^cron:[[:space:]]*[^n]' && echo "yes" || echo "no")
    # Pull up to 2 trigger phrases from eval.yaml for the message body.
    triggers=$(grep -oE '^\s+utterance:[[:space:]]*"[^"]+"' "$HOME/personal-assistant/user-skills/$slug/eval.yaml" 2>/dev/null \
      | head -2 \
      | sed -E 's/.*utterance:[[:space:]]*"([^"]+)".*/"\1"/' \
      | paste -sd ', ' -)
    if [ "$status" = "A" ]; then
      if [ "$is_periodic" = "yes" ]; then
        msg="🎉 Your new skill \`${slug}\` is live. It'll run automatically on its schedule. Say \`/refine ${slug}\` if you want to tweak it later."
      elif [ -n "$triggers" ]; then
        msg="🎉 Your new skill \`${slug}\` is live. Try saying ${triggers} — I'll know what to do now."
      else
        msg="🎉 Your new skill \`${slug}\` is live."
      fi
    elif [ "$status" = "M" ]; then
      msg="🔧 Refinement to \`${slug}\` is live. Existing triggers still work."
    else
      continue
    fi
    # Send via Telegram Bot API (HTTPS, no extra deps)
    local resp http_code
    resp=$(curl -s -o /tmp/tg-callback-$$.body -w "%{http_code}" \
      -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
      --data-urlencode "chat_id=${chat_id}" \
      --data-urlencode "text=${msg}" \
      --data-urlencode "parse_mode=Markdown" 2>/dev/null)
    if [ "$resp" = "200" ]; then
      echo "  ${green}✓${rst} telegram callback sent for ${slug} (${status})"
    else
      echo "  ${yellow}!${rst} telegram callback for ${slug} returned HTTP ${resp}"
      head -200 /tmp/tg-callback-$$.body 2>/dev/null | head -3
    fi
    rm -f /tmp/tg-callback-$$.body
  done <<< "$affected"
  return 0
}
step "telegram deploy-callback" notify_user_skill_deploys

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

# ── 5c. ensure ~/.brain/_raw/ is gitignored ──────────────────────
# Raw sidecars (verbatim rendered pages from browser-harness) live
# under ~/.brain/_raw/ and must NEVER be pushed to the GitHub brain
# repo — they can include X session signatures, login state hints,
# and content the source's ToS doesn't want mirrored publicly. The
# reading-list `git_commit` already only stages an explicit dir
# allowlist (so `_raw/` is safe by construction), but a manual
# `git add -A` from a user shell would commit the sidecar. Belt and
# braces: also list `_raw/` in ~/.brain/.gitignore.
ensure_brain_gitignore() {
  local f="$HOME/.brain/.gitignore"
  [ -d "$HOME/.brain" ] || { echo "  ${yellow}!${rst} ~/.brain missing — skipping"; return 0; }
  touch "$f"
  if ! grep -qxF "_raw/" "$f"; then
    echo "_raw/" >> "$f"
    echo "  ${green}✓${rst} added _raw/ to ~/.brain/.gitignore"
  else
    echo "  ${green}✓${rst} _raw/ already gitignored"
  fi
  return 0
}
step "ensure ~/.brain/_raw gitignored" ensure_brain_gitignore

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
