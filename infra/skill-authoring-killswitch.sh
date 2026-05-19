#!/bin/bash
# skill-authoring-killswitch.sh — emergency quarantine for user-skills/.
#
# Triggered by systemd ExecStartPre on the openclaw-gateway service. If
# the env var SKILL_AUTHORING_PANIC=1 is set when the unit starts (or
# restarts), move the entire user-skills/ overlay aside so the daemon
# comes back up with NO user-authored skills loaded. The originals in
# skills/ and openclaw/skills/ are unaffected (they aren't touched).
#
# Use this when:
#   • A malicious or buggy user-skill is causing the daemon to crash-loop
#     or misbehave and you can't pinpoint which one fast enough to fix
#     in place.
#   • You suspect indirect prompt injection landed a skill that wasn't
#     caught by the gates.
#   • You want a fast "panic stop" while you investigate.
#
# To trigger:
#   systemctl --user set-environment SKILL_AUTHORING_PANIC=1
#   systemctl --user restart openclaw-gateway
#
# To clear (after investigation):
#   systemctl --user unset-environment SKILL_AUTHORING_PANIC
#   systemctl --user restart openclaw-gateway
#   # the quarantined directories are still on disk under
#   # ~/.openclaw/workspace/skills/<name>.quarantined.<ts>/ — review
#   # individually and mv back into place if safe.
#
# The script is idempotent: re-running with the panic flag set already
# does nothing on subsequent passes (no user-skills/ to move aside).
# Without the panic flag set, it exits 0 immediately (no-op).

set -euo pipefail

WORKSPACE_SKILLS="${WORKSPACE_SKILLS:-$HOME/.openclaw/workspace/skills}"
TRACKER="${TRACKER:-$HOME/.openclaw/.user-skills-managed.list}"
LOG="${LOG:-$HOME/.brain-bootstrap-raw/logs/killswitch.log}"

log() {
  mkdir -p "$(dirname "$LOG")"
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG" >&2
}

if [ "${SKILL_AUTHORING_PANIC:-0}" != "1" ]; then
  # Normal path — no panic, nothing to do.
  exit 0
fi

log "SKILL_AUTHORING_PANIC=1 detected — quarantining user-skills/"

if [ ! -d "$WORKSPACE_SKILLS" ]; then
  log "workspace skills dir $WORKSPACE_SKILLS does not exist — nothing to quarantine"
  exit 0
fi

if [ ! -f "$TRACKER" ]; then
  log "tracker $TRACKER not found — can't identify Alfred-authored skills; bailing out (will NOT touch hand-authored skills/)"
  # This is the "fail safe" path: without the tracker we can't tell which
  # directories under workspace/skills/ are Alfred-authored vs hand-authored.
  # Moving them all aside would break the entire bot. Better to leave
  # everything alone and surface the issue.
  log "ACTION REQUIRED: tracker missing. Either restore it or manually quarantine the suspect user-skill via: mv $WORKSPACE_SKILLS/<name> $WORKSPACE_SKILLS/<name>.quarantined.\$(date +%s)"
  exit 0
fi

TS=$(date +%Y%m%d-%H%M%S)
QUARANTINED_COUNT=0

while IFS= read -r name; do
  [ -z "$name" ] && continue
  src="$WORKSPACE_SKILLS/$name"
  dst="$WORKSPACE_SKILLS/$name.quarantined.$TS"
  if [ -d "$src" ]; then
    mv "$src" "$dst" && {
      log "  quarantined $name → $dst"
      QUARANTINED_COUNT=$((QUARANTINED_COUNT + 1))
    } || log "  ::warning:: failed to mv $src → $dst"
  fi
done < "$TRACKER"

log "quarantined $QUARANTINED_COUNT user-skill(s); openclaw-gateway can start now"

# Don't fail the unit start. The whole point is to bring the bot back up
# with user-skills/ removed.
exit 0
