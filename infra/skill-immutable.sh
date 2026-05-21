#!/usr/bin/env bash
# skill-immutable.sh — enforce ext4 immutable bit on workspace
# user-skill files so the agent's `exec cat > .../SKILL.md` bypass
# is impossible at the kernel level (EPERM).
#
# Why this is the right fix:
#
#   Layers tried previously and why they were insufficient:
#     - Prompt-level BANNED_RESPONSES (#108, #109): model finds new
#       paths to bypass (user-skills/ -> skills/ -> workspace/skills/).
#     - Audit daemon revert (#110, #111, #112): catches the symptom
#       after the fact; user still sees a fake "Done" reply, audit
#       races against the next read, and a fast-firing skill could
#       run on rogue content for ~2 seconds.
#
#   Structural enforcement at the kernel:
#     - Linux ext4 immutable attribute (`chattr +i`) blocks ALL writes
#       to a file, even as root, until `chattr -i` is run. This is the
#       same mechanism sysadmins use for /etc/resolv.conf and similar
#       "should not be touched except through the proper tool" files.
#     - When the bot tries `cat > workspace/skills/X/SKILL.md`, the
#       kernel returns EPERM and the exec tool result shows the error.
#       The model sees the clear failure and stops trying.
#     - The ONLY way to write these files is via this helper
#       (`skill-immutable.sh write <src> <dst>`), which sudo-unlocks,
#       copies, and re-locks atomically.
#
# Sudo dependency: `sudo -n chattr` must work without password for
# the botuser. Verified on this VPS; if it ever breaks, the helper
# fails closed (logs error, leaves file mutable) and the existing
# audit-daemon revert takes over as fallback.
#
# Usage:
#   skill-immutable.sh lock          # lock all repo-known user-skill files in workspace
#   skill-immutable.sh unlock        # unlock all (for sync write)
#   skill-immutable.sh status        # show lsattr for each
#   skill-immutable.sh write src dst # unlock dst, cp src -> dst, re-lock
#   skill-immutable.sh lock-one SLUG # lock files for one specific slug
#   skill-immutable.sh unlock-one SLUG

set -uo pipefail

WORKSPACE_SKILLS="${WORKSPACE_SKILLS:-$HOME/.openclaw/workspace/skills}"
REPO="${REPO:-$HOME/personal-assistant}"
REPO_US="$REPO/user-skills"

list_slugs() {
  [ -d "$REPO_US" ] || return 0
  for d in "$REPO_US"/*/; do
    [ -d "$d" ] && [ -f "${d}SKILL.md" ] || continue
    basename "$d"
  done
}

lock_one_slug() {
  local slug="$1"
  for f in SKILL.md eval.yaml; do
    local path="$WORKSPACE_SKILLS/$slug/$f"
    [ -f "$path" ] || continue
    if sudo -n chattr +i "$path" 2>/dev/null; then
      echo "  locked: skills/$slug/$f"
    else
      echo "  WARN: failed to lock skills/$slug/$f (sudo chattr unavailable)" >&2
    fi
  done
}

unlock_one_slug() {
  local slug="$1"
  for f in SKILL.md eval.yaml; do
    local path="$WORKSPACE_SKILLS/$slug/$f"
    [ -f "$path" ] || continue
    if sudo -n chattr -i "$path" 2>/dev/null; then
      :  # silent on unlock (used in inner loops)
    else
      echo "  WARN: failed to unlock skills/$slug/$f" >&2
    fi
  done
}

status_one_slug() {
  local slug="$1"
  for f in SKILL.md eval.yaml; do
    local path="$WORKSPACE_SKILLS/$slug/$f"
    [ -f "$path" ] || continue
    lsattr "$path" 2>/dev/null | awk -v p="skills/$slug/$f" '{printf "  %-22s  %s\n", $1, p}'
  done
}

# Atomic write: unlock dst (if locked + exists), cp src -> dst, re-lock dst.
write_one() {
  local src="$1"
  local dst="$2"
  [ -f "$src" ] || { echo "skill-immutable: src missing: $src" >&2; return 1; }
  # Ensure parent dir exists.
  mkdir -p "$(dirname "$dst")"
  if [ -f "$dst" ]; then
    sudo -n chattr -i "$dst" 2>/dev/null || true
  fi
  cp "$src" "$dst" || { echo "skill-immutable: cp failed: $src -> $dst" >&2; return 1; }
  sudo -n chattr +i "$dst" 2>/dev/null || echo "  WARN: locked-write skipped (chattr failed) for $dst" >&2
}

# Best-effort lock attempt — try to lock every workspace file matching
# every repo slug. Run after sync_user_skills().
case "${1:-status}" in
  lock)
    for slug in $(list_slugs); do lock_one_slug "$slug"; done
    ;;
  unlock)
    for slug in $(list_slugs); do unlock_one_slug "$slug"; done
    ;;
  status)
    for slug in $(list_slugs); do status_one_slug "$slug"; done
    ;;
  write)
    write_one "${2:?usage: write <src> <dst>}" "${3:?usage: write <src> <dst>}"
    ;;
  lock-one)
    lock_one_slug "${2:?slug required}"
    ;;
  unlock-one)
    unlock_one_slug "${2:?slug required}"
    ;;
  *)
    echo "usage: $0 {lock|unlock|status|write <src> <dst>|lock-one <slug>|unlock-one <slug>}" >&2
    exit 2
    ;;
esac
