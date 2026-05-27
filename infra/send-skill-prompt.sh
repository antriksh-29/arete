#!/usr/bin/env bash
# send-skill-prompt.sh — deterministic send of a periodic-skill question
# message to the user, with the provenance tag + state-file marker that
# the reply router relies on.
#
# 2026-05-24 update: switched from single-slot state to APPEND-ONLY jsonl.
# Reason: video-recommender fired at 11:45 and wrote the marker; PG
# fired at 15:41 and OVERWROTE it. User's 16:00 reply to the 11:45
# message routed to PG (wrong) because the single slot held only the
# most recent cron's marker. Multi-slot keeps every cron's marker so
# the router can disambiguate by Telegram message_id (captured below
# from the sendMessage API response).
#
# State file: ${BRAIN_PATH}-bootstrap-raw/state/recent-skill-prompts.jsonl
# Format (one JSON object per line, append-only, oldest first):
#   {"slug":"...","sent_at":1779600000,"message_id":12345,"chat_id":"775188536"}
# We keep the last 30 entries (trim on every append). 30 ≈ enough for
# all reasonable cron fires within a multi-day window.
#
# Usage:
#   send-skill-prompt.sh <slug> "<question text>"
#
# Idempotent. Exits non-zero on Telegram send failure.

set -euo pipefail

SLUG="${1:-}"
BODY="${2:-}"
if [ -z "$SLUG" ] || [ -z "$BODY" ]; then
  echo "usage: send-skill-prompt.sh <slug> '<question text>'" >&2
  exit 2
fi

STATE_DIR="$HOME/.brain-bootstrap-raw/state"
RECENT_JSONL="$STATE_DIR/recent-skill-prompts.jsonl"
# Keep the legacy single-slot file in sync as a backstop for any
# consumer that hasn't migrated to the jsonl yet.
LEGACY_SINGLE="$STATE_DIR/last-periodic-skill.json"

mkdir -p "$STATE_DIR"

# HTML-escape via Python for safety.
esc() {
  python3 -c 'import html,sys;print(html.escape(sys.argv[1]),end="")' "$1"
}
SLUG_ESC=$(esc "$SLUG")
BODY_ESC=$(esc "$BODY")
# Footer used to leak the slug as a slash-command ("/${SLUG_ESC}") to the
# user. Friend-voice retune (identity/SOUL.md rule 5 + hard blocklist):
# slugs never reach chat. The router already disambiguates by Telegram
# reply message_id (jsonl multi-slot, see header comment), so the footer
# only needs to nudge the user to *reply to this message* — not to
# /-prefix the slug.
FOOTER="<i>↩ reply to this message to answer</i>"
MSG="${BODY_ESC}

${FOOTER}"

# Send via Telegram's sendMessage API directly so we can capture the
# message_id. send-telegram.sh discards the response and we need it.
TOKEN="$(/usr/local/bin/arete-cred get TELEGRAM_BOT_TOKEN)"
CHAT_ID="$(/usr/local/bin/arete-cred get TELEGRAM_USER_ID)"
if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
  echo "ABORT: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID missing from vault" >&2
  exit 1
fi

RESP=$(curl -sf -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MSG}" \
  --data-urlencode "parse_mode=HTML")

# Parse the message_id out of Telegram's response envelope:
#   {"ok":true, "result":{"message_id":12345, ...}}
MSG_ID=$(printf '%s' "$RESP" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("result", {}).get("message_id", ""))
except Exception:
    print("")
')

if [ -z "$MSG_ID" ]; then
  echo "WARN: Telegram sendMessage returned no message_id; state will be slug-only" >&2
fi

# Append to the jsonl AND update the legacy single-slot file. The
# legacy file is kept in sync for any consumer that hasn't migrated.
python3 - "$SLUG" "$BODY" "$MSG_ID" "$CHAT_ID" "$RECENT_JSONL" "$LEGACY_SINGLE" <<'PY'
import json, os, sys, time
slug, body, msg_id, chat_id, jsonl_path, legacy_path = sys.argv[1:7]
now = int(time.time())
entry = {
    "slug": slug,
    "sent_at": now,
    "message_id": int(msg_id) if msg_id.isdigit() else None,
    "chat_id": chat_id,
    "message_preview": body[:240],
}
# Append + trim to last 30.
existing = []
if os.path.isfile(jsonl_path):
    for line in open(jsonl_path):
        line = line.strip()
        if not line:
            continue
        try:
            existing.append(json.loads(line))
        except json.JSONDecodeError:
            pass
existing.append(entry)
existing = existing[-30:]
with open(jsonl_path, "w") as f:
    for e in existing:
        f.write(json.dumps(e) + "\n")
# Legacy single-slot.
json.dump({
    "slug": slug,
    "sent_at": now,
    "message_id": entry["message_id"],
    "message_preview": body[:240],
}, open(legacy_path, "w"), indent=2)
PY

echo "[$(date -Iseconds)] sent skill-prompt for /$SLUG (message_id=$MSG_ID)"
