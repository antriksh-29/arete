#!/usr/bin/env bash
# list-skill-crons.sh — enumerate every scheduled user-skill the agent
# knows about, regardless of which dispatcher owns it.
#
# Called by intent-router.sh's edit-verb firewall so when the user says
# "change the X timing to 8pm", the agent has a deterministic list of
# candidates to disambiguate against — rather than inventing a new
# skill (the 2026-05-23 failure).
#
# Three sources are unioned:
#   1. OpenClaw cron jobs    (~/.openclaw/cron/jobs.json)
#   2. Systemd user timers   (~/.config/systemd/user/skill-*.timer)
#   3. user-skills/*/SKILL.md frontmatter `cron:` field
#
# Output: one line per skill, "<slug>\t<schedule>\t<source>".
# Sorted by slug. Designed to be readable when displayed verbatim
# inside a Telegram message.

set -uo pipefail

JOBS_JSON="${JOBS_JSON:-$HOME/.openclaw/cron/jobs.json}"
SYSTEMD_DIR="${SYSTEMD_DIR:-$HOME/.config/systemd/user}"
USER_SKILLS="${USER_SKILLS:-$HOME/personal-assistant/user-skills}"

declare -A SCHED  # slug → "<expr>|<source>"

# Source 1: OpenClaw crons
if [ -f "$JOBS_JSON" ]; then
  while IFS=$'\t' read -r slug expr; do
    [ -z "$slug" ] && continue
    SCHED["$slug"]="${expr}|openclaw-cron"
  done < <(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for j in d.get("jobs", []):
    name = j.get("name") or ""
    expr = (j.get("schedule") or {}).get("expr") or "?"
    print(f"{name}\t{expr}")
' "$JOBS_JSON" 2>/dev/null)
fi

# Source 2: systemd user timers (skill-<slug>.timer)
if [ -d "$SYSTEMD_DIR" ]; then
  for t in "$SYSTEMD_DIR"/skill-*.timer; do
    [ -f "$t" ] || continue
    slug=$(basename "$t" .timer | sed 's/^skill-//')
    expr=$(grep -E '^OnCalendar=' "$t" 2>/dev/null | head -1 | sed 's/^OnCalendar=//')
    [ -z "$expr" ] && expr="?"
    # Prefer OpenClaw entry if both present (canonical for fetch skills);
    # otherwise add this.
    if [ -z "${SCHED[$slug]:-}" ]; then
      SCHED["$slug"]="${expr}|systemd-timer"
    fi
  done
fi

# Source 3: user-skills SKILL.md `cron:` frontmatter (covers skills
# whose dispatcher hasn't been registered yet).
if [ -d "$USER_SKILLS" ]; then
  for d in "$USER_SKILLS"/*/; do
    [ -f "${d}SKILL.md" ] || continue
    slug=$(basename "$d")
    cron_val=$(awk '/^---$/{c++} c==1 && /^cron:/{gsub(/^cron:[[:space:]]*"?|"?[[:space:]]*$/, "", $0); print; exit}' "${d}SKILL.md" 2>/dev/null | head -1)
    [ -z "$cron_val" ] || [ "$cron_val" = "null" ] && continue
    if [ -z "${SCHED[$slug]:-}" ]; then
      SCHED["$slug"]="${cron_val}|user-skills-md"
    fi
  done
fi

# Pretty print, sorted by slug, with a human-friendly time when the
# cron expression is a daily 5-field "M H * * *" pattern.
human_time() {
  local expr="$1"
  # Skip systemd OnCalendar — already human-readable.
  if [[ "$expr" == *":"* && "$expr" != *"*"* ]]; then
    echo "$expr"
    return
  fi
  read -r mm hh dom mon dow <<< "$expr"
  if [ "$dom" = "*" ] && [ "$mon" = "*" ] && [ "$dow" = "*" ] \
     && [[ "$hh" =~ ^[0-9]+$ ]] && [[ "$mm" =~ ^[0-9]+$ ]]; then
    printf 'daily %02d:%02d IST  (cron: %s)' "$hh" "$mm" "$expr"
  else
    echo "$expr"
  fi
}

if [ ${#SCHED[@]} -eq 0 ]; then
  echo "(no scheduled skills found)"
  exit 0
fi

for slug in $(printf '%s\n' "${!SCHED[@]}" | sort); do
  entry="${SCHED[$slug]}"
  expr="${entry%|*}"
  src="${entry##*|}"
  printf -- '- %-45s  %s  [%s]\n' "$slug" "$(human_time "$expr")" "$src"
done
