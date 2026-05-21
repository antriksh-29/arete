#!/usr/bin/env bash
# skill-autosync.sh — mirror workspace user-skill edits back to the
# personal-assistant git repo + push to GitHub, the same way
# ~/.brain syncs to antriksh-brain.
#
# Architectural shift from the immutable-bit approach (#113):
#
#   chattr +i failed because botuser has NOPASSWD sudo. The bot
#   ran `sudo chattr -i`, wrote, and continued. Wrong abstraction.
#
#   Brain pattern (works): scripts edit ~/.brain/ files freely,
#   then `cd ~/.brain && git commit && git push`. Trust the writer,
#   commit & push the change so it's visible everywhere.
#
#   This script applies the same pattern to user-skills:
#     - inotifywait on $WORKSPACE/skills/<user-skill-slug>/
#     - On change: copy workspace -> repo, update jobs.json cron if
#       SKILL.md `cron:` frontmatter changed, commit, push, ack
#       the user on Telegram with the GitHub commit URL.
#
# Failure modes addressed:
#   1. Bot edits workspace file -> previously: change lost on next
#      deploy. Now: pushed to GitHub within seconds.
#   2. Bot updates SKILL.md cron field but not jobs.json -> previously:
#      schedule didn't actually change. Now: jobs.json updated to
#      match SKILL.md frontmatter on every edit.
#   3. No Telegram confirmation -> previously: user couldn't tell if
#      the change actually persisted. Now: explicit message with
#      "Pushed to GitHub: <url>" link.
#
# Trust model: this script trusts whatever lands in the workspace
# files. The audit daemon (#110-#112) still runs as a tripwire — if
# anything goes wrong (failed safety check, malformed file), it can
# revert. For v1 we ship trust-and-push; future revs can layer
# per-edit safety scans before push.
#
# SSH key: ~/.ssh/github_personal_assistant must already be in the
# ssh-agent or git's IdentityFile config (it is — see ~/.ssh/config
# `Host github.com-personal-assistant`).
#
# Usage:
#   skill-autosync.sh --watch          # systemd-run forever
#   skill-autosync.sh --once           # one pass for slugs with drift

set -uo pipefail

REPO="${REPO:-$HOME/personal-assistant}"
WORKSPACE="${WORKSPACE:-$HOME/.openclaw/workspace}"
WORKSPACE_SKILLS="$WORKSPACE/skills"
REPO_US="$REPO/user-skills"
JOBS_JSON="${JOBS_JSON:-$HOME/.openclaw/cron/jobs.json}"
LOG="/tmp/skill-autosync.log"
TG_SEND="$HOME/bin/send-telegram.sh"
MODE="${1:---once}"

log() {
  local msg="[$(date -Iseconds)] $*"
  echo "$msg" | tee -a "$LOG" >&2
}

# Identify slugs that have repo + workspace counterparts
list_managed_slugs() {
  [ -d "$REPO_US" ] || return 0
  for d in "$REPO_US"/*/; do
    [ -f "${d}SKILL.md" ] || continue
    local slug
    slug=$(basename "$d")
    [ -d "$WORKSPACE_SKILLS/$slug" ] || continue
    echo "$slug"
  done
}

# Update jobs.json so a SKILL.md cron field change actually fires.
# Pulls the `cron:` value from SKILL.md frontmatter and rewrites the
# matching job's schedule.expr. Adds the job if it doesn't exist yet.
sync_cron_for_slug() {
  local slug="$1"
  local skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
  [ -f "$skill_md" ] || return 0
  [ -f "$JOBS_JSON" ] || return 0

  python3 - "$slug" "$skill_md" "$JOBS_JSON" <<'PY'
import json, re, sys, shutil, uuid
slug, skill_md, jobs_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Parse the `cron:` field out of SKILL.md frontmatter (between leading --- markers)
text = open(skill_md).read()
m = re.match(r"^---\n(.*?)\n---", text, re.S)
if not m:
    sys.exit(0)
fm = m.group(1)
cron_match = re.search(r'^cron:\s*"?([^"\n]+)"?\s*$', fm, re.M)
if not cron_match:
    sys.exit(0)
cron_val = cron_match.group(1).strip().strip('"')
if not cron_val or cron_val.lower() in ("null", "none"):
    # Manual skill — ensure any matching cron job is removed
    cron_val = None

jobs = json.load(open(jobs_path))
shutil.copy(jobs_path, jobs_path + ".bak-autosync")

# Find a job whose description references this slug
matching = [j for j in jobs["jobs"] if slug in (j.get("description") or "").lower()]
changed = False

if cron_val is None:
    # Manual mode: drop any matching job
    if matching:
        jobs["jobs"] = [j for j in jobs["jobs"] if j not in matching]
        changed = True
        print(f"removed cron entry for manual skill {slug}")
elif matching:
    job = matching[0]
    current = job.get("schedule", {}).get("expr")
    if current != cron_val:
        job["schedule"]["expr"] = cron_val
        # Bump description timestamp so the user sees the change reflected
        changed = True
        print(f"updated {slug} cron: {current} -> {cron_val}")
else:
    # Add a new cron entry for this slug
    jobs["jobs"].append({
        "id": str(uuid.uuid4()),
        "description": f"{slug} skill (auto-synced from SKILL.md)",
        "schedule": {"kind": "cron", "expr": cron_val, "tz": "Asia/Kolkata"},
        "prompt": f"Run the {slug} skill per user-skills/{slug}/SKILL.md.",
        "createdAt": "2026-05-21T00:00:00.000Z",
    })
    changed = True
    print(f"registered cron for {slug}: {cron_val}")

if changed:
    open(jobs_path, "w").write(json.dumps(jobs, indent=2))
PY
}

# Mirror one slug's workspace files into the repo. Returns 0 if anything
# changed, 1 if no changes. Doesn't push — caller batches push.
mirror_to_repo() {
  local slug="$1"
  local changed=0
  for f in SKILL.md eval.yaml; do
    local ws="$WORKSPACE_SKILLS/$slug/$f"
    local rp="$REPO_US/$slug/$f"
    [ -f "$ws" ] || continue
    if [ ! -f "$rp" ] || ! cmp -s "$ws" "$rp"; then
      cp "$ws" "$rp" && changed=1
    fi
  done
  return $((1 - changed))
}

# Commit & push all changes in $REPO_US. Returns 0 if pushed, 1 if no
# changes / push failed.
commit_and_push() {
  local commit_msg="$1"
  cd "$REPO" || return 1
  if git diff --quiet user-skills/ && git diff --cached --quiet user-skills/; then
    return 1  # nothing staged
  fi
  git add user-skills/ || return 1
  if git diff --cached --quiet; then
    return 1
  fi
  if ! git commit -m "$commit_msg" --quiet 2>&1 | tee -a "$LOG" >/dev/null; then
    log "commit failed"
    return 1
  fi
  if ! git push origin main --quiet 2>&1 | tee -a "$LOG" >/dev/null; then
    log "push failed — commit is local at $(git rev-parse HEAD)"
    return 1
  fi
  return 0
}

# Send a Telegram confirmation with the commit SHA and GitHub URL.
notify_user() {
  local slug="$1"
  local sha="$2"
  local cron_change="$3"   # "" or "18:30 -> 11:00"
  local repo_url="https://github.com/antriksh-29/personal-assistant"
  local commit_url="$repo_url/commit/$sha"
  local file_url="$repo_url/blob/main/user-skills/$slug/SKILL.md"

  local lines=(
    "✅ Updated user-skills/$slug — pushed to GitHub."
    ""
  )
  if [ -n "$cron_change" ]; then
    lines+=("Schedule: $cron_change")
    lines+=("")
  fi
  lines+=(
    "Commit: $commit_url"
    "File: $file_url"
  )
  local msg
  msg=$(printf '%s\n' "${lines[@]}")
  [ -x "$TG_SEND" ] && "$TG_SEND" "$msg" >/dev/null 2>&1 || true
}

# Compute a human-readable description of cron change for the user msg.
describe_cron_change() {
  local slug="$1"
  local before="$2"
  local after="$3"
  if [ "$before" = "$after" ]; then echo ""; return; fi
  # Express as `HH:MM <- HH:MM` for the daily case (5-field "M H * * *")
  local b_fmt a_fmt
  b_fmt=$(echo "$before" | awk '{if ($3=="*" && $4=="*" && $5=="*") printf "%02d:%02d", $2, $1; else print $0}')
  a_fmt=$(echo "$after" | awk '{if ($3=="*" && $4=="*" && $5=="*") printf "%02d:%02d", $2, $1; else print $0}')
  echo "$b_fmt IST → $a_fmt IST"
}

# Single pass: for each managed slug, mirror workspace -> repo, sync
# cron if changed, commit + push if anything changed. One commit per
# pass batches multiple slug changes if they happened at once.
one_pass() {
  local slugs_changed=()
  local cron_changes=()

  for slug in $(list_managed_slugs); do
    local before_cron after_cron
    before_cron=$(jq -r ".jobs[] | select(.description | test(\"$slug\"; \"i\")) | .schedule.expr" "$JOBS_JSON" 2>/dev/null | head -1)
    if mirror_to_repo "$slug"; then
      slugs_changed+=("$slug")
      sync_cron_for_slug "$slug"
      after_cron=$(jq -r ".jobs[] | select(.description | test(\"$slug\"; \"i\")) | .schedule.expr" "$JOBS_JSON" 2>/dev/null | head -1)
      local change_desc
      change_desc=$(describe_cron_change "$slug" "$before_cron" "$after_cron")
      cron_changes+=("$slug:$change_desc")
    fi
  done

  if [ ${#slugs_changed[@]} -eq 0 ]; then
    return 0
  fi

  local msg="user-skill autosync: $(IFS=, ; echo "${slugs_changed[*]}")"
  log "$msg"
  if commit_and_push "feat(user-skills): autosync from workspace — ${slugs_changed[*]}"; then
    local sha
    sha=$(cd "$REPO" && git rev-parse HEAD)
    log "pushed $sha"
    # Per-slug Telegram notification
    for entry in "${cron_changes[@]}"; do
      local slug="${entry%%:*}"
      local change="${entry#*:}"
      notify_user "$slug" "$sha" "$change"
    done
  else
    log "commit_and_push returned non-zero (may be a transient git/push error)"
  fi
}

watch_mode() {
  log "skill-autosync watch_mode start (repo=$REPO workspace=$WORKSPACE)"
  one_pass  # initial sync of any pending drift

  if ! command -v inotifywait >/dev/null 2>&1; then
    log "inotifywait missing — fallback to 30s polling"
    while true; do
      sleep 30
      one_pass
    done
  fi

  while true; do
    # Wait for any change in the workspace skills dir. -t 60 timeouts
    # so we still re-pass every minute as a safety net.
    inotifywait -r -e modify,close_write,moved_to,create --timeout 60 \
      "$WORKSPACE_SKILLS" 2>/dev/null | head -10 \
      | while read -r _ _; do : ; done
    # Debounce: brief delay so multi-file edits (SKILL.md + eval.yaml)
    # batch into a single commit.
    sleep 2
    one_pass
  done
}

case "$MODE" in
  --watch) watch_mode ;;
  --once|*) one_pass ;;
esac
