#!/usr/bin/env python3
# Approve / edit / reject the most recent /x-draft output. The pending draft
# lives at ~/.brain-bootstrap-raw/state/x-draft-pending.json.
#
# Usage:
#   x-approve.py approve
#   x-approve.py edit "<the edited tweet text>"
#   x-approve.py reject
#
# - approve  → append draft to corpus/x-antriksssh.md under "## Approved drafts"
# - edit     → store BOTH draft and edited version (the diff is the signal),
#              append edited to the approved-drafts section
# - reject   → log the rejection so future drafts can avoid the pattern
#
# All three clear the pending state file so the next draft starts fresh.

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

BRAIN_DIR = Path.home() / ".brain"
STATE_DIR = Path.home() / ".brain-bootstrap-raw" / "state"
STATE_FILE = STATE_DIR / "x-draft-pending.json"
FEEDBACK_LOG = STATE_DIR / "x-draft-feedback.jsonl"
OWN_CORPUS = BRAIN_DIR / "corpus" / "x-antriksssh.md"
GBRAIN = "/home/botuser/.bun/bin/gbrain"
TODAY = datetime.now(timezone.utc).date().isoformat()


def log(msg):
    print(f"[x-approve] {msg}", file=sys.stderr)


def load_pending():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log(f"failed to load pending: {e}")
        return None


def append_to_own_corpus(tweet_text, source_label):
    """Append an approved/edited tweet to the own-corpus brain page."""
    if not OWN_CORPUS.exists():
        log("own corpus missing — skipping append")
        return False
    body = OWN_CORPUS.read_text()
    entry = f"\n### {TODAY} — {source_label}\n> " + "\n> ".join(tweet_text.split("\n")) + "\n"

    if "## Approved drafts" in body:
        # Insert after the "## Approved drafts" header (and any placeholder paragraph)
        new_body = re.sub(
            r"(## Approved drafts\n\n(?:_[^_]*_\n\n)?)",
            r"\1" + entry + "\n",
            body,
            count=1,
        )
    else:
        new_body = body.rstrip() + "\n\n## Approved drafts\n" + entry
    OWN_CORPUS.write_text(new_body)
    subprocess.run([GBRAIN, "put", "corpus/x-antriksssh"], input=new_body, text=True, capture_output=True, check=False)
    log(f"appended approved entry to corpus/x-antriksssh ({source_label})")
    return True


def log_feedback(record):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


def clear_pending():
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: x-approve.py {approve|edit <text>|reject}")
    action = sys.argv[1]

    pending = load_pending()
    if not pending:
        print(json.dumps({"ok": False, "reason": "no_pending", "user_message": "📝 No draft pending. Generate one first with `draft a tweet about ...`."}))
        return

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "original_draft": pending.get("draft"),
        "mode": pending.get("mode"),
        "raw": pending.get("raw"),
        "style_override": pending.get("style_override"),
        "target": pending.get("target"),
    }

    if action == "approve":
        append_to_own_corpus(pending["draft"], f"approved (mode: {pending['mode']})")
        log_feedback(record)
        clear_pending()
        print(json.dumps({"ok": True, "user_message": f"✅ Approved and added to your voice corpus.\n\n{pending['draft']}"}))
        return

    if action == "edit":
        if len(sys.argv) < 3:
            sys.exit("Usage: x-approve.py edit \"<edited tweet text>\"")
        edited = sys.argv[2]
        record["edited_tweet"] = edited
        record["diff_chars"] = len(edited) - len(pending["draft"])
        append_to_own_corpus(edited, f"edited (mode: {pending['mode']}, original draft kept in feedback log)")
        log_feedback(record)
        clear_pending()
        msg = (
            f"✏️ Edit accepted and added to your voice corpus.\n"
            f"Original draft and your edit both logged so future drafts learn from the diff.\n\n"
            f"Final: {edited}"
        )
        print(json.dumps({"ok": True, "user_message": msg}))
        return

    if action == "reject":
        log_feedback(record)
        clear_pending()
        print(json.dumps({"ok": True, "user_message": "❌ Draft rejected. Logged as a negative example. Try a fresh angle?"}))
        return

    sys.exit(f"Unknown action: {action}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e), "user_message": "⚠️ Approval action failed. See logs."}))
        sys.exit(1)
