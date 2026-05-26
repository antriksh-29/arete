#!/usr/bin/env bash
# openclaw-telegram-probe-v10.sh — final pre-fix probe for Bug I.
#
# Resolves the ambiguities left by probe v9:
#   1. Exact `compaction.mode` enum values (truncated in v9 §3)
#   2. Per-agent compaction override path (agents.list[*].compaction.*?)
#   3. spawn-signal-detector.sh — what exactly does it do, and is the
#      session reusable or ephemeral by design?
#   4. Sample of signal-detector session content — what's accumulating?
#      (Lets us see whether ephemeral is viable.)
#   5. signal-detector's CURRENT agent config in agents.list — how
#      is it wired today?
#   6. Recent cost signal — successful compactions in journal with
#      token estimates.
#   7. Any "ephemeral session" knob in the schema (per-agent
#      session.persistent: false or similar)?
#   8. spawn-signal-detector.sh ownership + recent invocations
#
# Read-only. Safe. Output → stdout + /tmp/probe-v10.md

set -uo pipefail
OUT=/tmp/probe-v10.md
exec > >(tee "$OUT")
section() { echo; echo "## $1"; echo; }

echo "# probe-v10 — final pre-fix probe for Bug I"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. Full compaction.mode enum (untruncated)"
SCHEMA=$(mktemp)
openclaw config schema > "$SCHEMA" 2>/dev/null
jq '.properties.agents.properties.defaults.properties.compaction.properties.mode' "$SCHEMA" 2>&1
echo
echo "### plain list of allowed mode values:"
jq -r '.properties.agents.properties.defaults.properties.compaction.properties.mode.anyOf[]?.const // empty' "$SCHEMA" 2>&1

section "2. Per-agent compaction override path verification"
echo "### schema at agents.list[*].compaction:"
jq '.properties.agents.properties.list.items.properties.compaction // "(missing — overrides may live elsewhere)"' "$SCHEMA" 2>&1 | head -30
echo
echo "### Full list of keys allowed per agent in agents.list[*]:"
jq '.properties.agents.properties.list.items.properties | keys' "$SCHEMA" 2>&1

section "3. Existing signal-detector agent config"
echo "### the signal-detector entry in agents.list:"
openclaw config get agents.list 2>&1 | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    for i, agent in enumerate(data):
        if agent.get("name") == "signal-detector":
            print(f"[index {i}]")
            print(json.dumps(agent, indent=2))
            break
    else:
        print("(no agent named signal-detector found)")
except Exception as e:
    print(f"(parse err: {e})")
'

section "4. spawn-signal-detector.sh — the dispatch path"
SPAWN_SCRIPT="$HOME/bin/spawn-signal-detector.sh"
if [ -f "$SPAWN_SCRIPT" ]; then
  echo "### file mtime + size:"
  stat -c '%y %s' "$SPAWN_SCRIPT" 2>/dev/null || stat -f '%Sm %z' "$SPAWN_SCRIPT" 2>/dev/null
  echo
  echo "### contents (full):"
  cat "$SPAWN_SCRIPT"
else
  echo "(missing — $SPAWN_SCRIPT)"
fi

section "5. signal-detector session contents — what's accumulating?"
SD_DIR="$HOME/.openclaw/agents/signal-detector/sessions"
active=$(ls -t "$SD_DIR"/*.jsonl 2>/dev/null | grep -v -E '\.(trajectory|reset|checkpoint)\.' | head -1)
if [ -n "$active" ]; then
  echo "### first 3 entries (head — when session started):"
  head -3 "$active" 2>/dev/null | python3 -c '
import json, sys
for i, line in enumerate(sys.stdin):
    try:
        obj = json.loads(line)
        keys = list(obj.keys())
        kind = obj.get("type") or obj.get("role") or "?"
        ts = obj.get("timestamp") or obj.get("created_at") or "?"
        size = len(line)
        print(f"  [{i}] type={kind} ts={ts[:19]} keys={keys[:8]} size={size}B")
    except Exception as e:
        print(f"  [{i}] (parse err: {e})")
'
  echo
  echo "### last 3 entries (tail — current state):"
  tail -3 "$active" 2>/dev/null | python3 -c '
import json, sys
for i, line in enumerate(sys.stdin):
    try:
        obj = json.loads(line)
        keys = list(obj.keys())
        kind = obj.get("type") or obj.get("role") or "?"
        ts = obj.get("timestamp") or obj.get("created_at") or "?"
        size = len(line)
        print(f"  type={kind} ts={ts[:19]} keys={keys[:8]} size={size}B")
    except Exception as e:
        print(f"  (parse err: {e})")
'
  echo
  echo "### type-frequency breakdown across full session:"
  python3 -c '
import json
from collections import Counter
counts = Counter()
sizes = {}
total = 0
with open("'"$active"'") as f:
    for line in f:
        try:
            obj = json.loads(line)
            kind = obj.get("type") or obj.get("role") or "?"
            counts[kind] += 1
            sizes[kind] = sizes.get(kind, 0) + len(line)
            total += len(line)
        except Exception:
            pass
print(f"  total events: {sum(counts.values())}  total bytes: {total}")
for kind, n in counts.most_common(15):
    avg = sizes[kind] // n if n else 0
    pct = 100 * sizes[kind] / max(1, total)
    print(f"    {kind:30s} count={n:4d}  total_bytes={sizes[kind]:9d} ({pct:5.1f}%) avg={avg}B")
'
else
  echo "(no active signal-detector session)"
fi

section "6. Recent cost signal — successful compactions + token counts"
echo "### last 24h: count of compaction outcomes by type:"
journalctl --user -u openclaw-gateway --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -E '\[compaction-diag\] end' | \
  grep -oE 'sessionKey=[^ ]+ .*outcome=[a-z]+' | \
  awk '{print $1, $NF}' | sort | uniq -c | sort -rn | head -10
echo
echo "### last 24h: total compaction LLM time (sum of durationMs):"
journalctl --user -u openclaw-gateway --since '24 hours ago' --no-pager 2>/dev/null | \
  grep -oE 'durationMs=[0-9]+' | \
  awk -F= '{sum += $2; n++} END {if (n) printf "  total: %dms across %d attempts (%.1fs avg)\n", sum, n, sum/n/1000}'

section "7. Ephemeral-session knobs in schema"
echo "### Any 'persistent' / 'ephemeral' / 'oneShot' / 'sessionMode' under agents.list:"
jq -r '
  [paths | join(".")][]
  | select(test("persistent|ephemeral|oneShot|sessionMode|sessionPersistent"; "i"))
' "$SCHEMA" 2>&1 | sort -u | head -20
echo
echo "### per-agent session.* keys in schema:"
jq '.properties.agents.properties.list.items.properties.session // "(missing — no per-agent session config?)"' "$SCHEMA" 2>&1 | head -30

section "8. spawn-signal-detector.sh ownership + recent invocations"
echo "### binary in exec allowlist?"
python3 -c '
import json
try:
    with open("'"$HOME"'/.openclaw/exec-approvals.json") as f:
        data = json.load(f)
    patterns = [e.get("pattern","") for e in data.get("agents",{}).get("main",{}).get("allowlist",[])]
    hits = [p for p in patterns if "signal-detector" in p]
    print(f"  signal-detector entries in allowlist: {hits}")
except Exception as e:
    print(f"  (read err: {e})")
'
echo
echo "### last 5 signal-detector spawn events in journal:"
journalctl --user -u openclaw-gateway --since '6 hours ago' --no-pager 2>/dev/null | \
  grep -iE 'spawn-signal-detector|signal-detector.*spawn|exec.*signal-detector' | \
  tail -5

rm -f "$SCHEMA"
echo
echo "## Done"
echo "Report: $OUT ($(wc -l < "$OUT") lines)"
