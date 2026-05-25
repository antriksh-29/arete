#!/usr/bin/env bash
# skill-schedule.sh — return the live schedule of a user-skill as a
# friend-voice sentence, by reading the SKILL.md frontmatter directly.
#
# Why this exists: Bug C (2026-05-25). When the user asked "when do you
# send me messages for the video recommender every day?", the agent
# hallucinated "11:45am IST" — the cron is actually `50 16 * * *` =
# 4:50pm IST. The agent didn't read the file; it used stale session
# memory. Fix: force a deterministic file-read on this query class.
#
# The intent-router routes "when does X fire" / "what time do you send
# me X" queries to this script. The agent's job is just to pass through
# the slug fragment and pipe the output into say.sh.
#
# Source of truth: `~/personal-assistant/user-skills/<slug>/SKILL.md`
# frontmatter `cron:` field. The workspace-mirrored copy at
# `~/.openclaw/workspace/skills/<slug>/SKILL.md` is checked second as
# a fallback in case the repo path is missing on this host.
#
# Usage:
#   skill-schedule.sh <slug-or-fragment>
#   skill-schedule.sh --smoke
#   skill-schedule.sh --list           # all scheduled user-skills
#
# Examples:
#   skill-schedule.sh yc-launches-daily-briefings
#     → "your yc launches daily briefings is on for 4:15pm daily."
#   skill-schedule.sh video-recommender
#     → "your video recommender is on for 4:50pm daily."
#
# Output goes to stdout as a JSON envelope:
#   {"ok":true,"skill":"skill-schedule","slug":"...","cron":"...","user_message":"..."}

set -uo pipefail

REPO_USER_SKILLS="$HOME/personal-assistant/user-skills"
WORKSPACE_SKILLS="$HOME/.openclaw/workspace/skills"

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"skill-schedule","user_message":"skill-schedule smoke ok"}'
  exit 0
fi

if [ "${1:-}" = "--list" ]; then
  # List every scheduled user-skill with friendly time.
  result='[]'
  for d in "$REPO_USER_SKILLS"/*/; do
    [ -f "${d}SKILL.md" ] || continue
    name=$(basename "$d")
    cron=$(awk '/^cron:/ {sub(/^cron:[[:space:]]*"?/, ""); sub(/"[[:space:]]*$/, ""); print; exit}' "${d}SKILL.md" 2>/dev/null)
    [ -z "$cron" ] && continue
    echo "$name	$cron"
  done
  exit 0
fi

slug_fragment="${1:-}"
if [ -z "$slug_fragment" ]; then
  echo '{"ok":false,"reason":"missing_slug","user_message":"hmm, which one? (give me a hint, like \"yc launches\" or \"pg insights\")"}' >&2
  exit 2
fi

# Resolve fragment → exact slug. Multi-word fuzzy match: every
# word in the fragment (lowercased) must appear in the slug
# (lowercased, dashes treated as word boundaries). "video recommender"
# matches "create-a-personalized-video-recommender" because both
# "video" and "recommender" appear in it.
matched=""
match_count=0
candidates=""
match_result=$(python3 -c '
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

for m in $match_result; do
  matched="$m"
  match_count=$((match_count + 1))
  candidates="$candidates $m"
done

if [ "$match_count" -eq 0 ]; then
  python3 -c '
import json, sys
print(json.dumps({
    "ok": False,
    "reason": "no_match",
    "slug_fragment": sys.argv[1],
    "user_message": f"hmm, I don'"'"'t have a skill that matches \"{sys.argv[1]}\". want me to list what'"'"'s on?"
}, ensure_ascii=False))
' "$slug_fragment"
  exit 3
fi

if [ "$match_count" -gt 1 ]; then
  # Multiple matches — make the agent ask for clarification.
  python3 -c '
import json, sys
cands = sys.argv[2].strip().split()
friendly = [c.replace("-", " ") for c in cands]
msg = "a few of yours match \"" + sys.argv[1] + "\": " + ", ".join(friendly) + ". which one?"
print(json.dumps({
    "ok": False,
    "reason": "ambiguous",
    "slug_fragment": sys.argv[1],
    "matches": cands,
    "user_message": msg
}, ensure_ascii=False))
' "$slug_fragment" "$candidates"
  exit 3
fi

# Single match — read the cron field.
slug="$matched"
skill_md="$REPO_USER_SKILLS/$slug/SKILL.md"
if [ ! -f "$skill_md" ]; then
  # Fallback to workspace path
  skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
fi
if [ ! -f "$skill_md" ]; then
  echo '{"ok":false,"reason":"skill_md_missing","slug":"'"$slug"'","user_message":"that one isn'"'"'t set up here. weird, did it get removed?"}' >&2
  exit 4
fi

cron=$(awk '/^cron:/ {sub(/^cron:[[:space:]]*"?/, ""); sub(/"[[:space:]]*$/, ""); print; exit}' "$skill_md" 2>/dev/null)

# Translate 5-field cron to friend-voice. Handles the common daily
# case ("M H * * *") and a couple of weekly / hourly / interval shapes.
# Anything weirder falls through to a generic "on a custom schedule"
# response — better that than a wrong-translation lie.
python3 -c '
import json, re, sys
slug, cron = sys.argv[1], sys.argv[2]
friendly_slug = slug.replace("-", " ")

def to_12h(h, m):
    h = int(h); m = int(m)
    if h == 0:    return f"12:{m:02d}am"
    if h < 12:    return f"{h}:{m:02d}am"
    if h == 12:   return f"12:{m:02d}pm"
    return f"{h-12}:{m:02d}pm"

if not cron:
    msg = f"your {friendly_slug} is set up but doesn'"'"'t have a fixed schedule, so it only fires when you ask."
    print(json.dumps({"ok": True, "slug": slug, "cron": None, "user_message": msg}, ensure_ascii=False))
    sys.exit(0)

# Strip surrounding quotes if any
cron = cron.strip().strip("'"'"'").strip("\"")
fields = cron.split()
day_names = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

phrase = None
if len(fields) == 5:
    m, h, dom, mon, dow = fields
    if dom == "*" and mon == "*" and dow == "*" and m.isdigit() and h.isdigit():
        phrase = f"{to_12h(h, m)} daily"
    elif dom == "*" and mon == "*" and m.isdigit() and h.isdigit() and dow != "*":
        if dow.isdigit():
            phrase = "{} every {}".format(to_12h(h, m), day_names[int(dow) % 7])
        elif "," in dow:
            days = [day_names[int(x) % 7] for x in dow.split(",") if x.isdigit()]
            phrase = "{} every {}".format(to_12h(h, m), ", ".join(days))
    elif dom != "*" and mon == "*" and dow == "*" and m.isdigit() and h.isdigit() and dom.isdigit():
        phrase = f"{to_12h(h, m)} on the {dom}th of every month"
    elif m.isdigit() and h == "*" and dom == "*" and mon == "*" and dow == "*":
        phrase = f"every hour at :{int(m):02d}"
    elif h.startswith("*/") and m.isdigit() and dom == "*" and mon == "*" and dow == "*":
        phrase = f"every {h[2:]} hours"

if phrase:
    msg = f"your {friendly_slug} is on for {phrase}."
else:
    msg = f"your {friendly_slug} is on a custom schedule."

print(json.dumps({"ok": True, "slug": slug, "cron": cron, "user_message": msg}, ensure_ascii=False))
' "$slug" "$cron"
