#!/usr/bin/env bash
# skill-write-audit.sh — structural enforcement for the skill-author pipeline.
#
# The Telegram skill-author flow has a recurring failure mode: the LLM
# bypasses the elicit / safety-scan / routing-eval / commit-and-deploy
# pipeline by running raw `exec cat > .../skills/<slug>/SKILL.md`
# commands. Prompt-level BANNED_RESPONSES (#108, #109) have failed
# multiple times — the model finds a new path each time
# (user-skills/ → skills/ → workspace/skills/ → …).
#
# This script is the structural fix. It runs as a systemd timer
# (every 30s) AND continuously via inotifywait on the four skill
# directories. Any file in those directories that ISN'T tracked by
# git gets reverted (deleted). The bot's rogue writes survive for at
# most a few seconds before being purged.
#
# Allowed writers:
#   - `commit-and-deploy.ts` writes to the repo (`~/personal-assistant/{user-skills,skills,openclaw/skills}/`)
#     and then pushes to GitHub. The next deploy syncs the file into
#     workspace via `sync_user_skills()` in deploy-vps.sh. Both paths
#     end up tracked by git, so the audit lets them pass.
#   - `deploy-vps.sh` sync sets up the workspace mirror from the git
#     checkout. The audit treats anything matching a tracked path in
#     the repo as allowed in the workspace too.
#
# Everything else gets reverted. The bot's `exec cat > workspace/.../SKILL.md`
# fabricated-skill write becomes a no-op within seconds.
#
# Logs:
#   - /tmp/skill-write-audit.log (rolling, last 1000 lines)
#   - journalctl --user -u skill-write-audit
#
# Usage:
#   skill-write-audit.sh --once       # one pass, exit
#   skill-write-audit.sh --watch      # run forever via inotifywait
#   skill-write-audit.sh              # alias for --once (timer mode)

set -uo pipefail

REPO="${REPO:-$HOME/personal-assistant}"
WORKSPACE="${WORKSPACE:-$HOME/.openclaw/workspace}"
LOG="/tmp/skill-write-audit.log"
MODE="${1:---once}"

# Bound the log to the most recent 1000 lines so it doesn't grow unbounded.
log() {
  local msg="[$(date -Iseconds)] $*"
  echo "$msg" | tee -a "$LOG" >&2
  # Truncate periodically (~1% of writes) to keep size manageable.
  if [ $((RANDOM % 100)) -eq 0 ] && [ -f "$LOG" ]; then
    tail -1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}

# ──────────────────────────────────────────────────────────────────
# Pass 1 — repo-side: anything in skills/* not in `git ls-files` gets deleted.
# ──────────────────────────────────────────────────────────────────

audit_repo() {
  [ -d "$REPO/.git" ] || { log "REPO not a git repo: $REPO"; return; }
  cd "$REPO" || return

  # Authoritative tracked file list for skill paths.
  local tracked
  tracked=$(git ls-files 'skills/*' 'user-skills/*' 'openclaw/skills/*' 2>/dev/null | sort -u)

  # Current on-disk file list for the same paths.
  local found
  found=$(find skills user-skills openclaw/skills \
    -type f \( -name 'SKILL.md' -o -name 'eval.yaml' -o -name '*.usage.json' \) \
    2>/dev/null | sort -u)

  # comm -13 = lines only in $found (i.e. on disk but not in git).
  local extras
  extras=$(comm -13 <(echo "$tracked") <(echo "$found"))

  if [ -n "$extras" ]; then
    echo "$extras" | while IFS= read -r f; do
      [ -z "$f" ] && continue
      [ -f "$REPO/$f" ] || continue
      log "REVERT repo: $f (not in git ls-files)"
      rm -f "$REPO/$f"
      # If the parent slug dir is now empty, remove it too.
      local dir
      dir=$(dirname "$REPO/$f")
      if [ -d "$dir" ] && [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
        rmdir "$dir" 2>/dev/null && log "REVERT repo: rmdir $dir (was empty)"
      fi
    done
  fi
}

# ──────────────────────────────────────────────────────────────────
# Pass 2 — workspace user-skills/ only.
#
# IMPORTANT: We only audit `workspace/user-skills/`. The other two
# workspace tiers (`workspace/skills/`, `workspace/openclaw/skills/`)
# are MIXED — they contain both repo-synced skills AND plugin-installed
# runtime skills (e.g. `1password`, `brain-ops`, `enrich`, `xurl`,
# `trello` are installed by gbrain/openclaw plugins from bun cache,
# NOT from the repo). Reverting them would break native functionality.
#
# `workspace/user-skills/` is exclusively repo-mirrored — anything
# there that's not in `$REPO/user-skills/` is rogue.
# ──────────────────────────────────────────────────────────────────

audit_workspace() {
  [ -d "$WORKSPACE" ] || return
  local ws_dir="$WORKSPACE/user-skills"
  [ -d "$ws_dir" ] || return
  for slug_dir in "$ws_dir"/*/; do
    [ -d "$slug_dir" ] || continue
    local slug
    slug=$(basename "$slug_dir")
    # Skip zombie tombstones from prior cleanups.
    case "$slug" in *.zombie.*) continue ;; esac
    if [ ! -f "$REPO/user-skills/$slug/SKILL.md" ]; then
      log "REVERT workspace: user-skills/$slug (no corresponding $REPO/user-skills/$slug/SKILL.md)"
      rm -rf "$slug_dir"
    fi
  done
}

# ──────────────────────────────────────────────────────────────────
# Pass 3 — cron jobs: any `*.openclaw/cron/jobs.json` entry whose
# prompt is empty AND whose description matches a "skill-shaped" name
# is a CronCreate-bypass artifact. Delete.
# ──────────────────────────────────────────────────────────────────

audit_crons() {
  local jobs="$HOME/.openclaw/cron/jobs.json"
  [ -f "$jobs" ] || return
  local removed
  removed=$(python3 - <<'PY' "$jobs"
import json, sys, re
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    sys.exit(0)
keep, drop = [], []
SKILL_SHAPED = re.compile(
    r"(fact|insight|quote|tip|briefing|reminder|update|joke|lesson|idea|nugget|wisdom|essay|story|prompt|challenge|advice|thought|takeaway)",
    re.I,
)
for j in d.get("jobs", []):
    prompt = (j.get("prompt") or j.get("command") or "").strip()
    desc = (j.get("description") or "")
    if not prompt and SKILL_SHAPED.search(desc):
        drop.append(j)
    else:
        keep.append(j)
if drop:
    d["jobs"] = keep
    open(p, "w").write(json.dumps(d, indent=2))
    for j in drop:
        print(f"REVERT cron: {j.get('description', '?')}")
PY
  )
  if [ -n "$removed" ]; then
    echo "$removed" | while IFS= read -r line; do log "$line"; done
  fi
}

# ──────────────────────────────────────────────────────────────────
# One-shot pass
# ──────────────────────────────────────────────────────────────────

one_pass() {
  audit_repo
  audit_workspace
  audit_crons
}

# ──────────────────────────────────────────────────────────────────
# Watch mode (inotify) — installed as a long-running systemd service.
# Falls back to a 5-second polling loop if inotifywait isn't present.
# ──────────────────────────────────────────────────────────────────

watch_mode() {
  log "watch_mode start (repo=$REPO workspace=$WORKSPACE)"
  one_pass  # initial cleanup

  local watch_paths=(
    "$REPO/skills"
    "$REPO/user-skills"
    "$REPO/openclaw/skills"
    "$WORKSPACE/user-skills"
    # Intentionally NOT watching $WORKSPACE/skills/ or
    # $WORKSPACE/openclaw/skills/ — those are mixed-tier (repo + plugin
    # runtime) and we can't safely distinguish rogue writes from
    # plugin-installed skills without a roster.
  )
  # Filter to ones that actually exist.
  local existing=()
  for p in "${watch_paths[@]}"; do [ -d "$p" ] && existing+=("$p"); done

  if command -v inotifywait >/dev/null 2>&1 && [ ${#existing[@]} -gt 0 ]; then
    while true; do
      # -e create,modify,moved_to fires on the broadest set of write events.
      # -r recursive so nested SKILL.md writes are caught.
      # --timeout 30 so we still re-pass every 30s even when idle (defense
      # against missed events).
      inotifywait -r -e create,modify,moved_to --timeout 30 \
        --format '%w%f %e' "${existing[@]}" 2>/dev/null | head -50 \
        | while read -r _ _; do : ; done
      # Whether the inotify returned because of events or timeout, we
      # do a full audit pass. Cheap (git ls-files + a couple find calls).
      one_pass
    done
  else
    log "inotifywait not present — falling back to 5s polling loop"
    while true; do
      one_pass
      sleep 5
    done
  fi
}

case "$MODE" in
  --watch) watch_mode ;;
  --once|*) one_pass ;;
esac
