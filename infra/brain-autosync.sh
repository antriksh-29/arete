#!/usr/bin/env bash
# brain-autosync.sh — periodic auto-commit + push for ~/.brain/.
#
# Motivating bug (2026-05-22, image-v36): user asked the bot to modify
# the youtube-subscriptions-complete brain page. Either Kimi forgot to
# `git commit && git push` after the edit, or didn't make the edit at
# all. Local brain stayed in sync (or didn't), but GitHub mirror never
# saw the change. User noticed it later when checking the public link.
#
# Existing pattern: domain-specific scripts (reading-checkin.sh,
# water-log.sh, youtube-subs-categorize.py) each call `git add/commit/push`
# themselves. That works for THOSE scripts. It fails for any path that
# touches ~/.brain/ without going through them — including the agent
# editing files via raw exec.
#
# This script is the safety net: runs every 60s via systemd timer,
# `git status --porcelain` to detect any uncommitted changes, then
# `git add -A && git commit && git push`. Idempotent — no-op when
# nothing changed.
#
# Why minute-cadence instead of inotify: brain edits come from many
# sources (collector daemons, manual edits, agent exec, this and other
# scripts). inotify watchers race with each other. A simple polling
# loop converges deterministically without serialization complexity.
# Latency: ≤60s from edit to GitHub. Acceptable for a brain repo.
#
# Commit message: "alfred-autosync: <comma-separated changed files> @ <ts>"
# so the git history is informative without a separate commit per file.
#
# Conflict handling: if `git push` fails (concurrent push from a script
# that ran its own commit + push), do `git pull --rebase` and retry once.
# Brain repo is single-author so rebase is safe.

set -uo pipefail

BRAIN="${BRAIN:-$HOME/.brain}"
LOG="${LOG:-/tmp/brain-autosync.log}"
LOCK="${LOCK:-/tmp/brain-autosync.lock}"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG" >&2
}

# Single-instance lock so two timer ticks don't race
exec 9>"$LOCK"
if ! flock -n 9; then
  # Another instance already running — exit quietly
  exit 0
fi

if [ ! -d "$BRAIN/.git" ]; then
  log "no .git dir at $BRAIN — skipping"
  exit 0
fi

# Detect changes
cd "$BRAIN" || exit 0
changed=$(git status --porcelain 2>/dev/null)
if [ -z "$changed" ]; then
  # Clean state — quiet exit
  exit 0
fi

# Cap commit message at 5 changed paths for readability
files=$(echo "$changed" | awk '{$1=""; print}' | sed 's/^ //' | head -5 | xargs)
n_files=$(echo "$changed" | wc -l | tr -d ' ')
ts=$(date '+%Y-%m-%d %H:%M IST' 2>/dev/null || date)

# Stage everything — brain edits are always intentional (no .env / no
# build artifacts) so `add -A` is safe here. Verified ~/.brain/.gitignore
# already excludes non-content files.
git add -A 2>/dev/null

# Anything actually staged after the add?
staged=$(git diff --cached --name-only 2>/dev/null | head -1)
if [ -z "$staged" ]; then
  exit 0
fi

if [ "$n_files" -le 5 ]; then
  msg="alfred-autosync: $files @ $ts"
else
  msg="alfred-autosync: $files +$((n_files - 5)) more @ $ts"
fi

if ! git commit -m "$msg" >>"$LOG" 2>&1; then
  log "commit failed (probably nothing to commit after staging): see log"
  exit 0
fi

sha=$(git rev-parse --short HEAD 2>/dev/null)
log "committed $sha — $files"

# Push, with one rebase-and-retry on conflict
if git push >>"$LOG" 2>&1; then
  log "pushed $sha"
  exit 0
fi

log "push failed, attempting rebase + retry"
if git pull --rebase >>"$LOG" 2>&1 && git push >>"$LOG" 2>&1; then
  log "rebased + pushed $sha"
  exit 0
fi

log "ERROR: push failed twice (manual intervention needed). Latest commit $sha is local-only."
exit 1
