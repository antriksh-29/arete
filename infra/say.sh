#!/usr/bin/env bash
# say.sh — the ONLY allowed path for Alfred to send a user-facing message.
#
# Architecture (added 2026-05-24 with messages.visibleReplies=message_tool):
# OpenClaw is configured to suppress plain assistant.text emissions from
# reaching Telegram. The only way the user sees a reply is via this
# script (called as `exec ~/bin/say.sh "<text>"`) which is the bot's
# native send-message side-channel. Reasoning, planning narration, and
# tool-call decision text never leak — by framework design, not by
# prompt promise.
#
# Pipeline (every call):
#   1. humanizer.py --context chat  → strip em-dashes, AI vocab,
#                                     internal-state jargon, slugs,
#                                     cron strings, sycophancy.
#   2. reply-discipline.py          → catch NO_REPLY / banned non-reply
#                                     phrases / content-free acks.
#   3. send-telegram.sh             → Bot API call to Telegram.
#
# On gate FAIL: exit nonzero with the rejection envelope on stderr.
# The agent sees the rejection reason and can regenerate.
#
# Usage:
#   exec ~/bin/say.sh "moved your HN brief to 8:30pm. done."
#   exec ~/bin/say.sh -                  # read text from stdin
#   exec ~/bin/say.sh --html "<b>bold</b>" # HTML mode (pass-through to send-telegram.sh)
#   exec ~/bin/say.sh --context composio_relay "..."   # relay context (skips
#                                                       strict internal-jargon
#                                                       blocklist — use ONLY
#                                                       when relaying a tool's
#                                                       user_message that
#                                                       legitimately contains
#                                                       tech terms)
#   ~/bin/say.sh --smoke                # smoke test
#
# Exit codes:
#   0  — sent successfully
#   2  — humanizer regex error
#   3  — reply-discipline rejection (banned phrase / empty / content-free ack)
#   4  — Telegram send failed
#   64 — usage error

set -uo pipefail

# ── parse flags (short-circuit options before any helper checks) ──
CONTEXT="chat"           # humanizer context: chat | internal_status | composio_relay | research_answer
HTML_FLAG=""              # passed through to send-telegram.sh

while [ $# -gt 0 ]; do
  case "$1" in
    --smoke)
      echo '{"ok":true,"skill":"say","user_message":"say smoke ok"}'
      exit 0
      ;;
    --help|-h)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    --context)
      CONTEXT="${2:-chat}"
      shift 2
      ;;
    --context=*)
      CONTEXT="${1#--context=}"
      shift
      ;;
    --html)
      HTML_FLAG="--html"
      shift
      ;;
    *)
      break
      ;;
  esac
done

# ── locate the gate scripts (workspace OR ~/bin install) ──
# Deferred to after flag parsing so --smoke / --help short-circuit
# without requiring all helpers to be present on the dev machine.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

find_helper() {
  local name="$1"
  for cand in "$HOME/bin/$name" "$SCRIPT_DIR/$name" "$HOME/personal-assistant/infra/$name"; do
    # Existence is enough; the deploy ensures the +x bit. Some helpers
    # (e.g. send-telegram.sh) lack +x in the repo but get it on install.
    if [ -f "$cand" ]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

HUMANIZER=$(find_helper humanizer.py) || {
  echo '{"ok":false,"reason":"humanizer.py not found","user_message":"say.sh config error: humanizer.py missing"}' >&2
  exit 64
}
REPLY_DISCIPLINE=$(find_helper reply-discipline.py) || {
  echo '{"ok":false,"reason":"reply-discipline.py not found","user_message":"say.sh config error: reply-discipline.py missing"}' >&2
  exit 64
}
SEND_TELEGRAM=$(find_helper send-telegram.sh) || {
  echo '{"ok":false,"reason":"send-telegram.sh not found","user_message":"say.sh config error: send-telegram.sh missing"}' >&2
  exit 64
}

# ── read the text (arg or stdin) ──
if [ "${1:-}" = "-" ] || [ $# -eq 0 ]; then
  RAW=$(cat)
else
  RAW="$*"
fi

if [ -z "${RAW//[[:space:]]/}" ]; then
  echo '{"ok":false,"reason":"empty","user_message":"say.sh refused: empty text. Compose a real reply or stay silent (no say.sh call)."}' >&2
  exit 3
fi

# ── JSON envelope auto-extraction ──
#
# When the agent pipes a tool's full JSON envelope into say.sh
# (e.g. `~/bin/skill-author/elicit.ts ... | ~/bin/say.sh -`), the
# entire `{"ok":true,"user_message":"...","detail":{...}}` blob ends
# up as `$RAW`. Without extraction it would land in Telegram, which
# is exactly the 2026-05-24 IMG_9921 regression.
#
# Heuristic: if the input parses as a JSON object AND has a
# `user_message` field, extract that. Otherwise pass through. This
# preserves freeform text while making the pipe-envelope-direct
# pattern safe.
EXTRACTED=$(printf '%s' "$RAW" | python3 -c '
import json, sys
try:
    raw = sys.stdin.read().strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        sys.exit(2)  # not JSON-shaped, pass through
    obj = json.loads(raw)
    if isinstance(obj, dict) and "user_message" in obj:
        msg = obj["user_message"]
        if isinstance(msg, str):
            sys.stdout.write(msg)
            sys.exit(0)
    sys.exit(2)
except (json.JSONDecodeError, ValueError):
    sys.exit(2)
' 2>/dev/null)
EXTRACT_RC=$?
if [ $EXTRACT_RC -eq 0 ] && [ -n "${EXTRACTED//[[:space:]]/}" ]; then
  RAW="$EXTRACTED"
fi

# ── pipeline ──

# 1. humanizer (strip AI tells + internal-state jargon based on context)
HUMANIZED_JSON=$(printf '%s' "$RAW" | python3 "$HUMANIZER" --context "$CONTEXT" 2>&1)
if [ $? -ne 0 ]; then
  echo "say.sh: humanizer crashed:" >&2
  echo "$HUMANIZED_JSON" >&2
  exit 2
fi
HUMANIZED=$(printf '%s' "$HUMANIZED_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(d["output"])')

if [ -z "${HUMANIZED//[[:space:]]/}" ]; then
  echo '{"ok":false,"reason":"humanizer_stripped_everything","user_message":"say.sh refused: humanizer left nothing. The input was probably all banned tokens. Regenerate the reply with no internal-state references."}' >&2
  exit 3
fi

# 2. reply-discipline (catch NO_REPLY / banned phrases / content-free acks)
VERDICT_JSON=$(printf '%s' "$HUMANIZED" | python3 "$REPLY_DISCIPLINE" 2>&1)
if [ $? -ne 0 ]; then
  echo "say.sh: reply-discipline crashed:" >&2
  echo "$VERDICT_JSON" >&2
  exit 2
fi
OK=$(printf '%s' "$VERDICT_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("yes" if d.get("ok") else "no")')
if [ "$OK" != "yes" ]; then
  # Surface the rejection envelope to stderr so the agent can read the
  # reason and the suggestion field, then regenerate.
  echo "say.sh: reply-discipline rejected the reply:" >&2
  echo "$VERDICT_JSON" >&2
  exit 3
fi

# 3. send via Telegram Bot API (the side-channel that bypasses openclaw's
#    suppressed channel forwarder).
if [ -n "$HTML_FLAG" ]; then
  printf '%s' "$HUMANIZED" | bash "$SEND_TELEGRAM" "$HTML_FLAG" -
else
  printf '%s' "$HUMANIZED" | bash "$SEND_TELEGRAM" -
fi
SEND_RC=$?

if [ $SEND_RC -ne 0 ]; then
  echo "say.sh: Telegram send failed with exit $SEND_RC" >&2
  exit 4
fi

# Success envelope on stdout so the agent has a structured ack to log.
# Cheap to emit; downstream callers can ignore.
python3 -c '
import json, sys
print(json.dumps({
    "ok": True,
    "skill": "say",
    "context": sys.argv[1],
    "user_message": sys.argv[2],
}, ensure_ascii=False))
' "$CONTEXT" "$HUMANIZED"
