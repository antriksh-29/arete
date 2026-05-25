#!/usr/bin/env bash
# skill-describe.sh — friend-voice summary of a user-skill's capability
# + schedule + triggers, by reading SKILL.md frontmatter and body.
#
# Why this exists: Bug K (2026-05-25). When the user asked "tell me about
# my video recommender skill", the agent's reply was the raw SKILL.md
# file content (YAML frontmatter + markdown body). Two failures: (a)
# intent-router had no route for descriptive queries, so the agent
# composed freely; (b) humanizer had no rules to strip YAML so the
# leak passed through. This helper closes (a) by giving the agent a
# deterministic command to call that returns a friend-voice summary
# instead of a file dump.
#
# Companion to:
#   skill-schedule.sh — for "when does X fire?" (time-only queries)
#   propose-skill-edit.sh / apply-skill-edit.sh — for mutations
#
# Usage:
#   skill-describe.sh <slug-fragment>
#   skill-describe.sh --smoke
#
# Output: JSON envelope with friend-voice user_message field.

set -uo pipefail

REPO_USER_SKILLS="$HOME/personal-assistant/user-skills"

if [ "${1:-}" = "--smoke" ]; then
  echo '{"ok":true,"skill":"skill-describe","user_message":"skill-describe smoke ok"}'
  exit 0
fi

slug_fragment="${1:-}"
if [ -z "$slug_fragment" ]; then
  echo '{"ok":false,"reason":"missing_slug","user_message":"hmm, which one? (give me a hint, like \"video recommender\" or \"pg insights\")"}' >&2
  exit 2
fi

# Multi-word fuzzy match: same algorithm as skill-schedule.sh + propose-skill-edit.sh.
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
if [ ! -f "$skill_md" ]; then
  echo '{"ok":false,"reason":"skill_md_missing","user_message":"that one isn'"'"'t set up anymore. did it get removed?"}' >&2
  exit 4
fi

# Parse frontmatter + body, synthesize friend-voice summary.
# Uses Python for safe YAML parse (no external pyyaml — small hand
# parser) and friendly time conversion.
python3 - "$slug" "$skill_md" <<'PY'
import json, re, sys
slug = sys.argv[1]
path = sys.argv[2]

with open(path) as f:
    content = f.read()

# Split frontmatter from body
fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
if fm_match:
    fm_text, body = fm_match.group(1), fm_match.group(2)
else:
    fm_text, body = "", content

# Hand-parse the YAML frontmatter (just the keys we care about — no
# external pyyaml dep, keeps the script self-contained).
fields = {}
current_key = None
for line in fm_text.split("\n"):
    m = re.match(r'^([a-zA-Z_]+):\s*(.*)$', line)
    if m:
        current_key = m.group(1)
        val = m.group(2).strip()
        # Strip surrounding quotes
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        fields[current_key] = val
    elif current_key and line.startswith("  "):
        fields[current_key] = (fields.get(current_key, "") + " " + line.strip()).strip()

cron = fields.get("cron", "")
description = fields.get("description", "")
trigger = fields.get("trigger", "")
created_by = fields.get("created_by", "")

def cron_to_friendly(c):
    if not c: return None
    fs = c.split()
    if len(fs) != 5: return None
    m, h, dom, mon, dow = fs
    if dom == "*" and mon == "*" and dow == "*" and m.isdigit() and h.isdigit():
        h, mm = int(h), int(m)
        if h == 0:    return f"12:{mm:02d}am"
        if h < 12:    return f"{h}:{mm:02d}am"
        if h == 12:   return f"12:{mm:02d}pm"
        return f"{h-12}:{mm:02d}pm"
    return None

friendly_slug = slug.replace("-", " ")
schedule_friendly = cron_to_friendly(cron)

# Build friend-voice summary. Three optional pieces:
#   schedule (if cron set)
#   what it does (from description, truncated + cleaned)
#   how to trigger (manual triggers, if any)
parts = []

# Clean the description: strip the WHEN/NOT WHEN scaffolding that
# safety-scan adds, leaving just the capability sentence.
desc = description
desc = re.sub(r"\s*WHEN:.*$", "", desc, flags=re.DOTALL)
desc = re.sub(r"\s*NOT WHEN:.*$", "", desc, flags=re.DOTALL)
desc = re.sub(r"\s+", " ", desc).strip()
# Cap at ~200 chars so the reply stays short
if len(desc) > 200:
    desc = desc[:200].rsplit(" ", 1)[0] + "…"

if schedule_friendly and desc:
    msg = f"your {friendly_slug} runs at {schedule_friendly} daily. {desc}"
elif schedule_friendly:
    msg = f"your {friendly_slug} runs at {schedule_friendly} daily."
elif desc:
    msg = f"your {friendly_slug}: {desc}"
else:
    msg = f"your {friendly_slug} is set up but doesn't have a description on file."

# If body has a "## Capability" section, prefer that first paragraph
# (cleaner than the YAML description).
cap_match = re.search(r"##\s+Capability\s*\n+([^\n#]+(?:\n[^\n#]+)*)", body)
if cap_match and schedule_friendly:
    cap_text = re.sub(r"\s+", " ", cap_match.group(1)).strip()
    if 20 < len(cap_text) < 300:
        msg = f"your {friendly_slug} runs at {schedule_friendly} daily. {cap_text}"

print(json.dumps({
    "ok": True,
    "skill": "skill-describe",
    "slug": slug,
    "cron": cron,
    "user_message": msg,
}, ensure_ascii=False))
PY
