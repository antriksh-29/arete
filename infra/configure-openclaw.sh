#!/usr/bin/env bash
# configure-openclaw.sh — applies tuned OpenClaw config knobs to the
# live ~/.openclaw/openclaw.json on the VPS. Idempotent; takes a
# timestamped backup before each mutation.
#
# Why this exists: openclaw.json is sensitive (gateway auth token,
# provider profiles) so we don't commit it. But several knobs benefit
# from version-controlled tuning. This script applies the desired
# config in a way that:
#   1. Doesn't store the secrets in the repo
#   2. Is replayable on every deploy
#   3. Logs what changed so unexpected drift is visible
#
# Knobs currently managed:
#   agents.defaults.bootstrapTotalMaxChars  ← 200000 → 60000
#       Drops per-turn bootstrap context from ~50K tokens to ~15K tokens.
#       Keeps the bot well under gpt-5.4-mini's 200K TPM ceiling
#       (was hitting it every few turns and silent-hanging — bug #1
#       in docs/E2E-FINDINGS.md).
#
# Usage:
#   bash infra/configure-openclaw.sh          # apply (default)
#   bash infra/configure-openclaw.sh --check  # report current state, no writes
#   bash infra/configure-openclaw.sh --dry    # print what would change, no writes

set -uo pipefail

CONFIG="$HOME/.openclaw/openclaw.json"
MODE="apply"
case "${1:-}" in
  --check) MODE="check" ;;
  --dry)   MODE="dry" ;;
esac

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: $CONFIG not found — is OpenClaw installed?"
  exit 1
fi

# All knobs we manage. Format: "<dot.path>=<value>".
# python types: ints stay ints. Strings need quoting. Bools as JSON.
DESIRED=(
  # bootstrapMaxChars is the PER-FILE limit. AGENTS.md is ~48K chars
  # and was being truncated at 40K, cutting off daily-task-manager
  # routing rules (live at line 871+ of AGENTS.md) and silently
  # breaking the skill — bot would hallucinate "saved" replies
  # without actually invoking ~/bin/daily-tasks.sh. Bumped to 50000
  # so full AGENTS.md survives injection. Keep ~10K headroom over
  # the file's current size so adding routing rules doesn't re-break
  # truncation.
  "agents.defaults.bootstrapMaxChars=60000"
  # bootstrapTotalMaxChars is the WHOLE-bootstrap budget (AGENTS.md +
  # all loaded SKILL.md files). Bumped 60K → 100K so the full
  # AGENTS.md + a healthy chunk of SKILL.md content both fit. Still
  # well under gpt-5.4-mini's 200K TPM ceiling (the silent-hang
  # regression from bug #1).
  "agents.defaults.bootstrapTotalMaxChars=100000"

)

# messages.messagePrefix — managed as a here-string so multi-line is
# clean and we don't have to escape \n. POSITIVE-framed Kimi-friendly
# output discipline (the earlier "BANNED phrases" list backfired —
# Kimi K2.6 echoed the banned tokens). Re-runnable: this script
# overwrites VPS drift on every deploy.
MESSAGE_PREFIX="$(cat <<'PREFIX_EOF'
[SYSTEM PREAMBLE]
Your assistant turn output is EXACTLY ONE of:
  (a) A tool call.
  (b) The verbatim stdout (or user_message field) of the most recent tool call.
Nothing else. No planning narration. No "The user wants...", "I need to...", "Let me...", "I'll run...", "Now I should...". If you find yourself writing those, you have failed the turn — abort, emit the tool call instead.

First tool call MUST be: exec ~/bin/spawn-signal-detector.sh "<user message verbatim>"

Its stdout ends with an INTENT ROUTER OUTPUT block. Follow it deterministically:

- INTENT=chat → no router-mandated action; compose a normal reply per AGENTS.md / SKILL.md.
- INTENT=task_review → run ~/bin/daily-tasks.sh review --date <Y-M-D> --reply "<reply verbatim>". Relay stdout verbatim. Never claim "Applied 0 status updates" without a clean retry.
- INTENT=task_set → run ~/bin/daily-tasks.sh set with the task list. Relay stdout verbatim.
- INTENT=task_add → run ~/bin/daily-tasks.sh add. Relay stdout verbatim.
- INTENT=task_mark → run ~/bin/daily-tasks.sh mark / remove per recipe. Relay stdout verbatim.
- INTENT=task_backlog → run ~/bin/daily-tasks.sh backlog [--age-gte N]. Relay stdout VERBATIM. Do NOT compose from brain pages. Reply MUST start with "📋 Backlog —".
- INTENT=task_stats → run ~/bin/daily-tasks.sh stats --window <window>. Relay stdout VERBATIM. Reply MUST start with "📊 Stats —". Never count from memory.
- INTENT=task_history → run ~/bin/daily-tasks.sh history <range>. Relay stdout verbatim.
- INTENT=task_show → run ~/bin/daily-tasks.sh show <target>. Relay stdout VERBATIM with status emojis intact. Never add suffixes like "— pending" or categorize into "Work"/"Personal" sections.
- INTENT=booking → run python3 ~/bin/book-reservation.py "<user msg>". Relay envelope user_message verbatim.
- INTENT=booking_confirm → run python3 ~/bin/book-reservation.py --submit. Relay verbatim.
- INTENT=booking_pick_time → run python3 ~/bin/book-reservation.py --pick-time "<time>". Relay verbatim.
- INTENT=otp_relay → run book-reservation.py --resume "<otp>" or fill-form.py with the OTP. Never refuse.

Tool results are authoritative. If a script returns "no tasks for <date>", relay that — do not invent an empty list. If a script exits nonzero, relay stderr — do not fabricate success.

User message follows:
PREFIX_EOF
)"

python3 - "$CONFIG" "$MODE" "$MESSAGE_PREFIX" "${DESIRED[@]}" <<'PYEOF'
import datetime, json, shutil, sys
from pathlib import Path

config_path = Path(sys.argv[1])
mode = sys.argv[2]
message_prefix = sys.argv[3]
desired = sys.argv[4:]

# Append the prefix knob to desired list so it goes through the same
# diff + write machinery as the numeric knobs.
desired = list(desired) + [("messages.messagePrefix", message_prefix)]

with open(config_path) as f:
    cfg = json.load(f)

def get(obj, path):
    for k in path.split("."):
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj

def set_(obj, path, value):
    parts = path.split(".")
    for k in parts[:-1]:
        if k not in obj or not isinstance(obj[k], dict):
            obj[k] = {}
        obj = obj[k]
    obj[parts[-1]] = value

def parse_spec(spec):
    """Each entry is either a 'k=v' string (numeric/JSON-literal knobs)
    or a (path, value) tuple for pre-parsed string knobs like the
    message prefix."""
    if isinstance(spec, tuple):
        return spec
    path, raw_val = spec.split("=", 1)
    try:
        new_val = json.loads(raw_val)
    except json.JSONDecodeError:
        new_val = raw_val
    return path, new_val

changes = []
for spec in desired:
    path, new_val = parse_spec(spec)
    current = get(cfg, path)
    if current != new_val:
        changes.append((path, current, new_val))

if mode == "check":
    print(f"=== {config_path}")
    print(f"=== checking {len(desired)} managed knob(s)")
    for spec in desired:
        path, target = parse_spec(spec)
        current = get(cfg, path)
        tag = "✓" if current == target else "✗"
        # Truncate long string values in the check display
        cur_disp = repr(current)
        tgt_disp = repr(target)
        if len(cur_disp) > 80: cur_disp = cur_disp[:80] + "..."
        if len(tgt_disp) > 80: tgt_disp = tgt_disp[:80] + "..."
        print(f"  {tag} {path} = {cur_disp}  (target {tgt_disp})")
    sys.exit(0 if not changes else 1)

if not changes:
    print(f"✓ openclaw.json already matches desired config ({len(desired)} knob(s) checked)")
    sys.exit(0)

print(f"=== {len(changes)} change(s) {'would be' if mode == 'dry' else 'will be'} applied")
for path, current, new_val in changes:
    cur_disp = repr(current)
    tgt_disp = repr(new_val)
    if len(cur_disp) > 80: cur_disp = cur_disp[:80] + "..."
    if len(tgt_disp) > 80: tgt_disp = tgt_disp[:80] + "..."
    print(f"  {path}: {cur_disp} → {tgt_disp}")

if mode == "dry":
    sys.exit(0)

# Backup before mutating
backup = config_path.with_suffix(config_path.suffix + f".bak.{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}")
shutil.copy(config_path, backup)
print(f"  backup: {backup}")

for path, _, new_val in changes:
    set_(cfg, path, new_val)

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"✓ wrote {config_path}")
print()
print("NOTE: openclaw-gateway must be restarted for config changes to take effect.")
print("       systemctl --user restart openclaw-gateway.service")
PYEOF
