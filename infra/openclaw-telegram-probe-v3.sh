#!/usr/bin/env bash
# openclaw-telegram-probe-v3.sh — verify PR #140 actually deployed and
# diagnose the regressions seen 2026-05-24 PM (silence on chat, JSON
# envelope leak, hallucinated cron times, bot saying "done" without
# editing the file).
#
# Read-only. Safe. Output → stdout + /tmp/openclaw-telegram-probe-v3.md

set -uo pipefail
OUT=/tmp/openclaw-telegram-probe-v3.md
exec > >(tee "$OUT")

section() { echo; echo "## $1"; echo; }

echo "# openclaw-telegram-probe-v3 — post-PR140 verification"
echo
echo "_Generated: $(date -Iseconds) on $(hostname)_"

section "1. Did messages.visibleReplies actually flip to message_tool?"
openclaw config get messages.visibleReplies 2>&1
echo "(expect: \"message_tool\". If still \"automatic\" or null, PR #140's configure-openclaw.sh didn't apply on the last deploy.)"

section "2. Is ~/bin/say.sh installed?"
if [ -x "$HOME/bin/say.sh" ]; then
  echo "✓ exists and is executable: $HOME/bin/say.sh ($(stat -c %s "$HOME/bin/say.sh" 2>/dev/null) bytes)"
  echo
  echo "### smoke test:"
  bash "$HOME/bin/say.sh" --smoke 2>&1
else
  echo "✗ MISSING from \$HOME/bin/say.sh — deploy didn't sync it"
  ls -la "$HOME/bin/say.sh" 2>&1
fi

section "3. Is say.sh in the exec allowlist?"
if [ -f "$HOME/.openclaw/exec-approvals.json" ]; then
  python3 -c '
import json
with open("'"$HOME"'/.openclaw/exec-approvals.json") as f:
    data = json.load(f)
patterns = [e.get("pattern","") for e in data.get("agents",{}).get("main",{}).get("allowlist",[])]
say_patterns = [p for p in patterns if "say.sh" in p]
print(f"matches for say.sh: {say_patterns}")
'
else
  echo "(no exec-approvals.json found)"
fi

section "4. Did the messagePrefix actually get the new wording?"
prefix=$(openclaw config get messages.messagePrefix 2>/dev/null | head -c 2000)
if echo "$prefix" | grep -q "say.sh"; then
  echo "✓ messagePrefix references say.sh"
  echo "first 500 chars:"
  echo "$prefix" | head -c 500
  echo
else
  echo "✗ messagePrefix does NOT mention say.sh — old preamble still in effect"
  echo "first 500 chars:"
  echo "$prefix" | head -c 500
fi

section "5. Workspace SKILL.md state vs repo (Bug C/D verification)"
for slug in yc-launches-daily-briefings create-a-personalized-video-recommender; do
  echo "### $slug"
  echo
  WORKSPACE="$HOME/.openclaw/workspace/user-skills/$slug/SKILL.md"
  REPO="$HOME/personal-assistant/user-skills/$slug/SKILL.md"
  for path in "$WORKSPACE" "$REPO"; do
    if [ -f "$path" ]; then
      cron=$(grep -E '^cron:' "$path" | head -1)
      mtime=$(date -r "$path" -Iseconds 2>/dev/null || stat -c %y "$path" 2>/dev/null)
      echo "- \`$path\`: $cron · mtime $mtime"
    else
      echo "- \`$path\`: ✗ MISSING"
    fi
  done
  echo
done

section "6. jobs.json cron entries (the live schedule openclaw fires from)"
if [ -f "$HOME/.openclaw/jobs.json" ]; then
  python3 -c '
import json
with open("'"$HOME"'/.openclaw/jobs.json") as f:
    data = json.load(f)
for job in data.get("jobs", []):
    desc = job.get("description","")
    expr = job.get("schedule",{}).get("expr","")
    if any(s in desc.lower() for s in ["yc","video","recommender","paul-graham","pg "]):
        print(f"- {expr}  ←  {desc[:120]}")
' 2>&1 | head -20
else
  echo "(no jobs.json found)"
fi

section "7. Recent journal for the failed turns (silence on chat, JSON leak)"
echo "### last 100 telegram-channel log lines, filtering for outbound/inbound message events:"
journalctl --user -u openclaw-gateway -n 800 --no-pager 2>/dev/null | \
  grep -iE '\[telegram\] (Inbound|Outbound|Send|reply|sent|skip)|say\.sh|message_tool|visibleReplies' | \
  tail -30

echo
echo "### last 50 ERROR/WARN lines:"
journalctl --user -u openclaw-gateway -n 500 --no-pager 2>/dev/null | \
  grep -iE '(error|warn|fail|silentReply)' | \
  tail -20

section "8. Does openclaw silentReply / silentReplyRewrite have a fallback we could use?"
openclaw config get agents.defaults.silentReply 2>&1
echo
openclaw config get agents.defaults.silentReplyRewrite 2>&1

section "9. Find hardcoded 'What should the result look like' string in skill-author"
grep -rn "What should the result look like" \
  "$HOME/personal-assistant/meta/skill-author/" \
  "$HOME/.openclaw/workspace/meta/skill-author/" 2>/dev/null | head -10 || \
  echo "(no matches found)"

section "10. Find JSON-envelope output paths (the elicit Bug B source)"
echo "### Scripts that emit JSON envelopes with user_message field:"
grep -rln '"user_message":' \
  "$HOME/personal-assistant/infra/" \
  "$HOME/personal-assistant/meta/" 2>/dev/null | head -15

echo
echo "## Done"
echo
echo "Report saved to: $OUT ($(wc -l < "$OUT") lines)"
