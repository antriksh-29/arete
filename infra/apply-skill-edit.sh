#!/usr/bin/env bash
# apply-skill-edit.sh — second phase of the quarantine pattern. Reads
# the most recent pending edit from the state dir, applies it to the
# workspace SKILL.md, then triggers skill-autosync to mirror to the
# repo + commit + push. Closes Bug D from 2026-05-25 (agent said
# "done" without actually editing the file).
#
# Called by the intent-router when the user replies "yes" (or any
# affirmative shape) to a pending skill-edit proposal. If no pending
# edit exists within the TTL window, this script reports that gently —
# the agent should then treat the message as normal chat.
#
# Idempotent: applying a pending edit consumes (deletes) the file, so
# accidental re-applies just no-op.
#
# Usage:
#   apply-skill-edit.sh                 # apply latest pending edit
#   apply-skill-edit.sh <slug>          # apply pending edit for specific slug
#   apply-skill-edit.sh --smoke
#   apply-skill-edit.sh --list          # show pending edits

set -uo pipefail

REPO="${REPO:-$HOME/personal-assistant}"
WORKSPACE_SKILLS="${WORKSPACE:-$HOME/.openclaw/workspace}/skills"
PENDING_DIR="$HOME/.brain-bootstrap-raw/state/pending-skill-edits"
AUTOSYNC="$REPO/infra/skill-autosync.sh"

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"apply-skill-edit","user_message":"apply-skill-edit smoke ok"}'
  exit 0
fi

if [ "${1:-}" = "--list" ]; then
  ls -1t "$PENDING_DIR"/*.json 2>/dev/null | head -20 || echo "(no pending edits)"
  exit 0
fi

# Pick the pending file: explicit slug arg, or the most recently mtimed.
target_slug="${1:-}"
if [ -n "$target_slug" ]; then
  pending="$PENDING_DIR/$target_slug.json"
else
  pending=$(ls -1t "$PENDING_DIR"/*.json 2>/dev/null | head -1)
fi

if [ -z "$pending" ] || [ ! -f "$pending" ]; then
  echo '{"ok":false,"reason":"no_pending","user_message":"hmm, I don'"'"'t have a pending change to confirm. did the proposal time out (15 min)? say what you want changed again."}' >&2
  exit 3
fi

# Read + validate the pending file. Refuse if expired.
parsed=$(python3 -c '
import json, sys, time
with open(sys.argv[1]) as f:
    p = json.load(f)
slug = p.get("slug")
change = p.get("change", {})
expires = p.get("expires_at", 0)
now = int(time.time())
if not slug or not change or change.get("field") != "cron":
    print("ERROR", "malformed_pending", flush=True)
    sys.exit(2)
if now > expires:
    print("ERROR", "expired", flush=True)
    sys.exit(3)
print("OK", slug, change.get("from") or "", change.get("to") or "", flush=True)
' "$pending" 2>&1)
status=$(echo "$parsed" | awk '{print $1}')
if [ "$status" = "ERROR" ]; then
  reason=$(echo "$parsed" | awk '{print $2}')
  if [ "$reason" = "expired" ]; then
    rm -f "$pending"
    echo '{"ok":false,"reason":"expired","user_message":"that change request timed out (over 15 min ago). say what you want changed again and I'"'"'ll re-propose."}' >&2
  else
    echo '{"ok":false,"reason":"malformed","user_message":"the pending change file is corrupted, I dropped it. say what you want changed again."}' >&2
    rm -f "$pending"
  fi
  exit 3
fi

slug=$(echo "$parsed" | awk '{print $2}')
from_cron=$(echo "$parsed" | cut -d' ' -f3-7)
to_cron=$(echo "$parsed" | cut -d' ' -f8-12)

# Locate the workspace SKILL.md (autosync's primary canonical source)
workspace_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
repo_md="$REPO/user-skills/$slug/SKILL.md"

target_md=""
if [ -f "$workspace_md" ]; then
  target_md="$workspace_md"
elif [ -f "$repo_md" ]; then
  # Fall back to the repo path (e.g. on a host where workspace sync hasn't run yet)
  target_md="$repo_md"
fi

if [ -z "$target_md" ]; then
  echo '{"ok":false,"reason":"skill_md_missing","user_message":"weird, I can'"'"'t find that skill'"'"'s file anymore. did it get removed?"}' >&2
  rm -f "$pending"
  exit 4
fi

# Edit the frontmatter cron field in-place using python (safer than sed
# for YAML-shaped front matter, especially around quoting).
python3 -c '
import sys, re
path = sys.argv[1]
new_cron = sys.argv[2]
with open(path) as f:
    content = f.read()

# Match the cron field inside frontmatter only (before the second ---)
# Frontmatter shape: starts with "---\n", contains "cron: ...", ends with "\n---".
lines = content.split("\n")
in_frontmatter = False
frontmatter_done = False
for i, line in enumerate(lines):
    if line.strip() == "---":
        if not in_frontmatter:
            in_frontmatter = True
            continue
        else:
            frontmatter_done = True
            break
    if in_frontmatter and not frontmatter_done and line.startswith("cron:"):
        lines[i] = f"cron: \"{new_cron}\""
        break
else:
    # No cron field in frontmatter — append it before the closing ---
    for i, line in enumerate(lines):
        if line.strip() == "---" and i > 0:
            lines.insert(i, f"cron: \"{new_cron}\"")
            break

with open(path, "w") as f:
    f.write("\n".join(lines))
' "$target_md" "$to_cron"

# Kick autosync (if available) to mirror the workspace edit → repo →
# commit → push immediately. Without this kick, the inotify watcher
# would catch the edit on its own within seconds, but the explicit
# call makes the apply-then-confirm flow synchronous from the user's
# perspective.
sync_status="ok"
if [ -x "$AUTOSYNC" ]; then
  if ! bash "$AUTOSYNC" --once >>/tmp/skill-autosync.log 2>&1; then
    sync_status="autosync_failed"
  fi
fi

# Consume the pending file (idempotency).
rm -f "$pending"

# Friend-voice success envelope. Use the same friendly-time renderer
# as skill-schedule.sh for consistency.
python3 -c '
import json, sys
slug, cron, sync = sys.argv[1:4]
def to_12h(s):
    fs = s.split()
    if len(fs) != 5: return None
    m, h, dom, mon, dow = fs
    if dom == "*" and mon == "*" and dow == "*" and m.isdigit() and h.isdigit():
        h, m = int(h), int(m)
        if h == 0:    return f"12:{m:02d}am"
        if h < 12:    return f"{h}:{m:02d}am"
        if h == 12:   return f"12:{m:02d}pm"
        return f"{h-12}:{m:02d}pm"
    return None
fr = to_12h(cron)
friendly_slug = slug.replace("-", " ")
if sync == "ok":
    if fr:
        msg = f"moved your {friendly_slug} to {fr}. done."
    else:
        msg = f"updated your {friendly_slug}. done."
else:
    if fr:
        msg = f"updated your {friendly_slug} to {fr} on my end, but the GitHub push hit a snag. it should retry in a bit."
    else:
        msg = f"updated your {friendly_slug} on my end, but the GitHub push hit a snag. it should retry in a bit."
print(json.dumps({
    "ok": True, "skill": "apply-skill-edit", "slug": slug,
    "cron": cron, "sync": sync, "user_message": msg
}, ensure_ascii=False))
' "$slug" "$to_cron" "$sync_status"
