#!/usr/bin/env bash
# seed-exec-allowlist.sh — pre-approve every routing-skill backing
# script in OpenClaw's exec-approval allowlist so user replies route
# without an interactive prompt that may go unseen.
#
# Why: the bot routes a Telegram reply (e.g. "yes" after the 10am
# reading check-in) by calling the matching skill's backing script.
# If the script's path isn't in
# ~/.openclaw/exec-approvals.json defaults / agents allowlist,
# OpenClaw fires an exec-approval prompt. If the prompt isn't
# attended, the reply is silently dropped. (See May 2026 incident:
# multiple "yes/no" replies to reading-checkin never landed in
# the brain because the script wasn't pre-approved.)
#
# Idempotent. Safe to run anytime, including from deploy-vps.sh.
#
# Usage:
#   bash infra/seed-exec-allowlist.sh         # add allowlist entries
#   bash infra/seed-exec-allowlist.sh --check  # report-only (exit 1 if any missing)

set -uo pipefail

EXEC_FILE="$HOME/.openclaw/exec-approvals.json"
SCRIPTS=(
  "$HOME/bin/reading-checkin.sh"
  "$HOME/bin/water-log.sh"
  "$HOME/bin/daily-tasks.sh"
  "$HOME/bin/activity-tracker.sh"
  "$HOME/bin/reading-list.py"
  "$HOME/bin/weather-snapshot.py"
  "$HOME/bin/usage-status.py"
  "$HOME/bin/skillify.py"
  "$HOME/bin/skill-drift-report.sh"
  "$HOME/bin/identity-audit.py"
  "$HOME/bin/humanizer.py"
  "$HOME/bin/reply-discipline.py"
  "$HOME/bin/agent-browser-status.sh"
  "$HOME/bin/image-ocr.py"
  "$HOME/bin/ingest-pdf.py"
  "$HOME/bin/ingest-x-thread.py"
  "$HOME/bin/x-draft.py"
  "$HOME/bin/x-approve.py"
  # 2026-05-24 — say.sh is the single allowed user-facing send path
  # under messages.visibleReplies=message_tool. Every turn ends with
  # an `exec ~/bin/say.sh ...` call, so it MUST be pre-approved or
  # every reply would prompt for exec approval.
  "$HOME/bin/say.sh"
  # 2026-05-25 — skill-schedule helper + two-phase mutation pattern
  # (Bug C + Bug D). All three need pre-approval since the intent-router
  # routes user turns to them and any approval prompt would silently
  # drop the user's request.
  "$HOME/bin/skill-schedule.sh"
  "$HOME/bin/propose-skill-edit.sh"
  "$HOME/bin/apply-skill-edit.sh"
)

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ ! -f "$EXEC_FILE" ]; then
  echo "✗ $EXEC_FILE not found — is OpenClaw initialised?" >&2
  exit 1
fi

# Use Python to safely mutate the JSON (jq might not be present).
python3 - "$EXEC_FILE" "$CHECK_ONLY" "${SCRIPTS[@]}" <<'PYEOF'
import json, sys, time, uuid
path = sys.argv[1]
check_only = sys.argv[2] == "1"
scripts = sys.argv[3:]

with open(path) as f:
    data = json.load(f)

agents = data.setdefault("agents", {})
main = agents.setdefault("main", {"allowlist": []})
allowlist = main.setdefault("allowlist", [])
existing = {entry.get("pattern") for entry in allowlist}

now_ms = int(time.time() * 1000)
added = []
already = []
for s in scripts:
    if s in existing:
        already.append(s)
    else:
        added.append(s)

if check_only:
    if added:
        print(f"✗ {len(added)} script(s) missing from allowlist:")
        for s in added:
            print(f"   - {s}")
        sys.exit(1)
    print(f"✓ all {len(scripts)} routing-skill scripts present in allowlist")
    sys.exit(0)

# Mutate path. Add the missing entries.
for s in added:
    allowlist.append({
        "pattern": s,
        "lastUsedAt": now_ms,
        "id": str(uuid.uuid4()),
    })

with open(path, "w") as f:
    json.dump(data, f, indent=2)

print(f"+ {len(added)} added · {len(already)} already present")
for s in added:
    print(f"  + {s}")
PYEOF
