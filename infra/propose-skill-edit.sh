#!/usr/bin/env bash
# propose-skill-edit.sh — write a pending skill-mutation to the state
# dir, but DO NOT apply it. Two-phase apply (the quarantine pattern,
# closes Bug D from 2026-05-25). The agent is supposed to call this
# script when the user asks to change a skill's schedule (or trigger,
# or any other field). It returns a friend-voice "moving your X to Y.
# cool?" message the agent pipes to say.sh, then STOPS.
#
# Only after the user replies "yes" does intent-router route them to
# apply-skill-edit.sh, which reads the pending file and actually
# performs the mutation. The agent CANNOT mutate SKILL.md directly —
# that's the architectural guarantee Bug D needs.
#
# State file: ~/.brain-bootstrap-raw/state/pending-skill-edits/<slug>.json
# TTL: 15 minutes (apply-skill-edit.sh refuses stale pending files).
#
# Usage:
#   propose-skill-edit.sh <slug-fragment> --change cron --to "30 13 * * *"
#   propose-skill-edit.sh <slug-fragment> --change cron --to-time "1:30pm"   # parsed
#   propose-skill-edit.sh --smoke
#   propose-skill-edit.sh --list   # show pending edits

set -uo pipefail

REPO_USER_SKILLS="$HOME/personal-assistant/user-skills"
PENDING_DIR="$HOME/.brain-bootstrap-raw/state/pending-skill-edits"
TTL_SECONDS=900   # 15 minutes

mkdir -p "$PENDING_DIR"

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"propose-skill-edit","user_message":"propose-skill-edit smoke ok"}'
  exit 0
fi

if [ "${1:-}" = "--list" ]; then
  ls -1 "$PENDING_DIR"/*.json 2>/dev/null | head -20 || echo "(no pending edits)"
  exit 0
fi

slug_fragment="${1:-}"
shift || true
field=""
to_value=""
to_time=""

while [ $# -gt 0 ]; do
  case "$1" in
    --change) field="${2:-}"; shift 2;;
    --to)     to_value="${2:-}"; shift 2;;
    --to-time) to_time="${2:-}"; shift 2;;
    *) shift;;
  esac
done

if [ -z "$slug_fragment" ] || [ -z "$field" ]; then
  echo '{"ok":false,"reason":"usage","user_message":"propose-skill-edit usage error"}' >&2
  exit 64
fi

# Resolve slug fragment using the same multi-word fuzzy match as
# skill-schedule.sh — every word in the fragment must appear in the
# slug.
resolved=$(python3 -c '
import sys, os, glob
frag = sys.argv[1].lower().strip()
words = [w for w in frag.replace("-", " ").replace("_", " ").split() if w]
base = sys.argv[2]
matches = []
for d in sorted(glob.glob(os.path.join(base, "*/"))):
    name = os.path.basename(os.path.normpath(d))
    if not os.path.isfile(os.path.join(d, "SKILL.md")):
        continue
    name_norm = name.lower().replace("-", " ").replace("_", " ")
    if all(w in name_norm for w in words):
        matches.append(name)
print("\n".join(matches))
' "$slug_fragment" "$REPO_USER_SKILLS")

match_count=$(echo "$resolved" | grep -c '^[^[:space:]]' || true)
if [ "$match_count" -eq 0 ]; then
  python3 -c '
import json, sys
print(json.dumps({
    "ok": False, "reason": "no_match",
    "user_message": f"hmm, I don'"'"'t have a skill that matches \"{sys.argv[1]}\". want me to list what'"'"'s on?"
}, ensure_ascii=False))
' "$slug_fragment"
  exit 3
fi

if [ "$match_count" -gt 1 ]; then
  python3 -c '
import json, sys
cands = sys.argv[2].strip().split()
friendly = [c.replace("-", " ") for c in cands]
print(json.dumps({
    "ok": False, "reason": "ambiguous", "matches": cands,
    "user_message": "a few of yours match \"" + sys.argv[1] + "\": " + ", ".join(friendly) + ". which one?"
}, ensure_ascii=False))
' "$slug_fragment" "$resolved"
  exit 3
fi

slug=$(echo "$resolved" | head -1)
skill_md="$REPO_USER_SKILLS/$slug/SKILL.md"

# Read current cron
current_cron=$(awk '/^cron:/ {sub(/^cron:[[:space:]]*"?/, ""); sub(/"[[:space:]]*$/, ""); print; exit}' "$skill_md" 2>/dev/null)

# Convert --to-time (e.g. "1:30pm") to cron if needed
if [ -z "$to_value" ] && [ -n "$to_time" ]; then
  to_value=$(python3 -c '
import re, sys
s = sys.argv[1].lower().replace(" ", "")
m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", s)
if not m: sys.exit(1)
h = int(m.group(1)); mm = int(m.group(2) or 0); ampm = m.group(3)
if ampm == "pm" and h < 12: h += 12
if ampm == "am" and h == 12: h = 0
if 0 <= h < 24 and 0 <= mm < 60:
    print(f"{mm} {h} * * *")
' "$to_time")
fi

if [ -z "$to_value" ]; then
  echo '{"ok":false,"reason":"missing_target","user_message":"hmm, I couldn'"'"'t parse the new time. try \"1:30pm\" or \"13:30\"."}' >&2
  exit 4
fi

# Only `cron` field is supported in v1. Other fields (triggers,
# capability) require the full elicit/refine pipeline.
if [ "$field" != "cron" ]; then
  echo '{"ok":false,"reason":"unsupported_field","user_message":"that kind of change needs the longer skill-author flow. want me to start it?"}' >&2
  exit 4
fi

# Build the pending JSON
now=$(date +%s)
expires=$((now + TTL_SECONDS))
pending="$PENDING_DIR/$slug.json"

python3 -c '
import json, sys
slug, current, new, now, expires = sys.argv[1:6]

def cron_to_friendly(c):
    if not c: return None
    fs = c.split()
    if len(fs) != 5: return None
    m, h, dom, mon, dow = fs
    if dom == "*" and mon == "*" and dow == "*" and m.isdigit() and h.isdigit():
        h, m = int(h), int(m)
        if h == 0:    return f"12:{m:02d}am"
        if h < 12:    return f"{h}:{m:02d}am"
        if h == 12:   return f"12:{m:02d}pm"
        return f"{h-12}:{m:02d}pm"
    return None

current_friendly = cron_to_friendly(current) or current or "(unset)"
new_friendly = cron_to_friendly(new) or new
friendly_slug = slug.replace("-", " ")

if new_friendly:
    summary = f"moving your {friendly_slug} to {new_friendly}. cool?"
else:
    summary = f"moving your {friendly_slug}. cool?"

envelope = {
    "ok": True,
    "skill": "propose-skill-edit",
    "slug": slug,
    "change": {"field": "cron", "from": current, "to": new},
    "proposed_at": int(now),
    "expires_at": int(expires),
    "summary": summary,
    "user_message": summary,
}

with open(sys.argv[6], "w") as f:
    json.dump(envelope, f, indent=2)

print(json.dumps(envelope, ensure_ascii=False))
' "$slug" "$current_cron" "$to_value" "$now" "$expires" "$pending"
