#!/usr/bin/env bash
# install-pre-push-hook.sh — installs a git pre-push hook that runs
# `bash eval/run-all-evals.sh --no-llm` and blocks the push if the
# gate fails. Idempotent. Safe to re-run.
#
# Why a hook (not a CI gate)? This repo deploys to a Hetzner VPS via
# git pull on the box; there's no GitHub Actions in the loop. The
# pre-push hook is the only place we can enforce gate-green-on-push
# without wiring an external runner.
#
# Usage:
#   bash infra/install-pre-push-hook.sh             # install
#   bash infra/install-pre-push-hook.sh --check     # report status
#   bash infra/install-pre-push-hook.sh --uninstall # remove

set -uo pipefail
cd "$(dirname "$0")/.."

# Use the git-common-dir so the hook works across Conductor worktrees
# (where .git is a file pointing into ../.git/worktrees/<name>/, but
# hooks/ lives in the shared common dir).
GIT_COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || echo .git)"
HOOK_DIR="$GIT_COMMON_DIR/hooks"
HOOK_PATH="$HOOK_DIR/pre-push"
MARKER="# managed by infra/install-pre-push-hook.sh"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'

# ── --check ─────────────────────────────────────────────────────
if [ "${1:-}" = "--check" ]; then
  if [ -f "$HOOK_PATH" ] && grep -q "$MARKER" "$HOOK_PATH" 2>/dev/null; then
    echo "${green}✓${rst} pre-push hook installed at $HOOK_PATH"
    exit 0
  fi
  echo "${yellow}!${rst} pre-push hook NOT installed"
  echo "  install with: bash infra/install-pre-push-hook.sh"
  exit 1
fi

# ── --uninstall ─────────────────────────────────────────────────
if [ "${1:-}" = "--uninstall" ]; then
  if [ -f "$HOOK_PATH" ] && grep -q "$MARKER" "$HOOK_PATH" 2>/dev/null; then
    rm "$HOOK_PATH"
    echo "${green}✓${rst} removed $HOOK_PATH"
  else
    echo "${yellow}!${rst} no managed pre-push hook found at $HOOK_PATH"
  fi
  exit 0
fi

# ── install (default) ───────────────────────────────────────────

if ! git rev-parse --git-common-dir >/dev/null 2>&1; then
  echo "${red}✗${rst} not a git repo"
  exit 1
fi

mkdir -p "$HOOK_DIR"

# If a non-managed hook already exists, refuse to overwrite — surface
# what's there so the user can decide.
if [ -f "$HOOK_PATH" ] && ! grep -q "$MARKER" "$HOOK_PATH" 2>/dev/null; then
  echo "${red}✗${rst} $HOOK_PATH already exists and is NOT managed by this installer."
  echo "  current contents:"
  sed 's/^/    /' "$HOOK_PATH"
  echo
  echo "  to replace with the managed version:"
  echo "    rm $HOOK_PATH && bash $0"
  exit 1
fi

cat > "$HOOK_PATH" <<'HOOK'
#!/usr/bin/env bash
# managed by infra/install-pre-push-hook.sh — do not hand-edit.
# Runs the audit gate before every push. Blocks if any step fails.
#
# Bypass (rare; document why in commit message): git push --no-verify

set -uo pipefail

# Find repo root (hook runs in any cwd within the repo)
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Skip if the gate runner doesn't exist yet (e.g. checking out an old
# commit). This isn't us being lenient — it's that the gate IS the
# contract; if it doesn't exist, there's nothing to enforce.
if [ ! -f eval/run-all-evals.sh ]; then
  echo "[pre-push] eval/run-all-evals.sh not present in this commit — skipping gate"
  exit 0
fi

echo "[pre-push] running audit gate (eval/run-all-evals.sh --no-llm) ..."

if bash eval/run-all-evals.sh --no-llm > /tmp/pre-push-gate.log 2>&1; then
  echo "[pre-push] ✓ gate green"
  exit 0
fi

# Gate failed — show the tail so the user knows what to fix.
echo "[pre-push] ✗ gate FAILED. Tail of /tmp/pre-push-gate.log:"
tail -40 /tmp/pre-push-gate.log
echo
echo "[pre-push] full log: /tmp/pre-push-gate.log"
echo "[pre-push] fix the issue, or push with --no-verify if absolutely necessary."
exit 1
HOOK

chmod +x "$HOOK_PATH"

echo "${green}✓${rst} installed pre-push hook at $HOOK_PATH"
echo
echo "Test it:    bash $HOOK_PATH < /dev/null"
echo "Bypass:     git push --no-verify   # rare; document why"
echo "Uninstall:  bash infra/install-pre-push-hook.sh --uninstall"
