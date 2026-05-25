#!/usr/bin/env bash
# openclaw-bug-H-verify.sh — post-deploy verification for PR #143.
#
# Checks that the Bug H fix actually landed in production:
#   1. compaction-notifier hook is `disabled` (not `ready`)
#   2. agents.defaults.compaction.maxActiveTranscriptBytes is "80mb"
#   3. humanizer.py has the new rotation-stripping rules
#   4. Any compaction events in the journal since deploy → no Telegram outbound
#
# Read-only. Safe. Output → stdout + /tmp/bug-H-verify.md.

set -uo pipefail
OUT=/tmp/bug-H-verify.md
exec > >(tee "$OUT")

section() { echo; echo "## $1"; echo; }

echo "# Bug H verification report"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. Is compaction-notifier disabled?"
echo "Looking at \`openclaw hooks list\` for the status column."
echo
openclaw hooks list 2>&1 | grep -iE 'compaction|status|hook|═|─' | head -20
echo
echo "### Verdict:"
if openclaw hooks list 2>&1 | grep -iE 'compaction-notifier' | grep -qiE 'disabled|off'; then
  echo "✓ compaction-notifier is DISABLED. Fix landed."
elif openclaw hooks list 2>&1 | grep -iE 'compaction-notifier' | grep -qiE 'ready|enabled|✓'; then
  echo "✗ compaction-notifier is still READY. Deploy didn't apply the hook disable, OR the hooks subsystem re-enabled it."
else
  echo "? Could not parse hook state. Check raw output above."
fi

section "2. Is the maxActiveTranscriptBytes knob set?"
openclaw config get agents.defaults.compaction.maxActiveTranscriptBytes 2>&1
echo
echo "(expect: \"80mb\". If different or missing, configure-openclaw.sh didn't apply.)"

section "3. Is humanizer.py updated?"
H="$HOME/bin/humanizer.py"
if [ -f "$H" ]; then
  echo "### file mtime: $(stat -c %y "$H" 2>/dev/null || stat -f %Sm "$H" 2>/dev/null)"
  echo
  echo "### rotation-related rules in humanizer.py:"
  grep -nE 'compaction_emoji|compaction_conversation|vocab_rotated_session|compaction_brain_pages' "$H" 2>/dev/null
  echo
  echo "### live regex test against the literal leaked message:"
  echo '🔄 I rotated to a fresh session — your conversation had grown to 134 messages, close to the model'\''s context limit. Earlier conversation is saved so I can recall it from the brain pages.' | python3 "$H" --context chat 2>&1 | python3 -c 'import json,sys; r=json.load(sys.stdin); print("INPUT  length:", len(r["input"])); print("OUTPUT length:", len(r["output"])); print("OUTPUT text:", repr(r["output"][:200]))'
else
  echo "✗ ~/bin/humanizer.py missing"
fi

section "4. Recent journal events for compaction (any leaked outbound?)"
echo "### last 30 'compaction' or 'rotat' or 'main-session' events:"
journalctl --user -u openclaw-gateway -n 1000 --no-pager 2>/dev/null | \
  grep -iE 'compaction|main-session-rotation|session.rotation|main-session-restart' | \
  tail -20
echo
echo "### Any outbound Telegram messages mentioning 'rotated' or 'compaction'?"
journalctl --user -u openclaw-gateway -n 2000 --no-pager 2>/dev/null | \
  grep -iE '\[telegram\].*(rotated|compaction|fresh session|grown to)' | \
  tail -10 || echo "(none — clean)"

section "5. Last deploy step status"
journalctl --user -u openclaw-gateway -n 500 --no-pager 2>/dev/null | \
  grep -iE 'compaction-notifier|disable_compaction' | head -10
echo
echo "(also try: tail -30 /tmp/deploy-vps.log if a local copy exists)"

section "6. Bonus — stalled-session warnings since the deploy (noticed in probe v4)"
journalctl --user -u openclaw-gateway --since '15 minutes ago' --no-pager 2>/dev/null | \
  grep -E 'stalled session' | wc -l | xargs printf "stalled-session warnings in the last 15 min: %s\n"

echo
echo "## Done"
echo "Report saved to: $OUT"
