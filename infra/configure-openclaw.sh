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

python3 - "$CONFIG" "$MODE" "${DESIRED[@]}" <<'PYEOF'
import datetime, json, shutil, sys
from pathlib import Path

config_path = Path(sys.argv[1])
mode = sys.argv[2]
desired = sys.argv[3:]

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

changes = []
for spec in desired:
    path, raw_val = spec.split("=", 1)
    # Try parsing as JSON literal (int / bool / string)
    try:
        new_val = json.loads(raw_val)
    except json.JSONDecodeError:
        new_val = raw_val
    current = get(cfg, path)
    if current != new_val:
        changes.append((path, current, new_val))

if mode == "check":
    print(f"=== {config_path}")
    print(f"=== checking {len(desired)} managed knob(s)")
    for spec in desired:
        path, raw_val = spec.split("=", 1)
        try: target = json.loads(raw_val)
        except: target = raw_val
        current = get(cfg, path)
        tag = "✓" if current == target else "✗"
        print(f"  {tag} {path} = {current!r}  (target {target!r})")
    sys.exit(0 if not changes else 1)

if not changes:
    print(f"✓ openclaw.json already matches desired config ({len(desired)} knob(s) checked)")
    sys.exit(0)

print(f"=== {len(changes)} change(s) {'would be' if mode == 'dry' else 'will be'} applied")
for path, current, new_val in changes:
    print(f"  {path}: {current!r} → {new_val!r}")

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
