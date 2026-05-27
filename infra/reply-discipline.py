#!/usr/bin/env python3
# Reply-discipline filter — runs before every outbound Telegram message.
#
# Role: a deterministic gate that catches the failure modes that prompted
# the recent NO_REPLY / banned-phrase / empty-ack fix commits. If the
# candidate reply is unsafe, return ok=false with a rewrite suggestion;
# the caller decides whether to retry or push the suggestion through.
#
# This is a constraint skill — it doesn't fire on user intent, it fires
# on every outbound message. Cheap (sub-millisecond). No network. No LLM.
#
# Usage:
#   echo "<candidate reply>" | reply-discipline.py
#   reply-discipline.py --smoke
#
# Exit code: always 0 (the JSON envelope's ok field signals pass/fail).

import json
import re
import sys


# ── rules ──────────────────────────────────────────────────────

# Substring patterns that should never appear in a user-facing reply.
# These fire when the model emits the literal sentinel or any banned
# non-reply phrase.
BANNED_PATTERNS = [
    (r"\bNO_REPLY\b", "literal NO_REPLY sentinel — never emit"),
    (r"no extra message needed", "non-reply phrase — produce a real reply"),
    (r"nothing more from me", "non-reply phrase — produce a real reply"),
    (r"no reply needed", "non-reply phrase — produce a real reply"),
    (r"i have nothing (more |else )?to (say|add)", "non-reply phrase"),
    (r"the (sub-)?agent handled (it|this)",
     "the sub-agent never replies; main agent owes a real reply"),
]

# Single-word acks that carry no information at all. Only trip if
# the WHOLE reply is one of these (with optional trailing punctuation).
#
# History: this list used to include "done", "noted", "captured" — but
# under friend-voice (see identity/SOUL.md rule 4), bare action-confirming
# words like "done.", "moved it.", "sent.", "on it." are the TARGET
# register, not the anti-pattern. They tie to a concrete action the
# main agent just took, and the user prefers them over verbose status
# reports. Only purely content-free acks ("ok", "k", "ack") that don't
# reference an action stay banned.
EMPTY_ACKS = ["ack", "ok", "okay", "k"]


# ── checks ─────────────────────────────────────────────────────

def check(reply: str) -> dict:
    """Return {ok, reason, suggestion} for the candidate reply."""
    stripped = reply.strip()

    if not stripped:
        return {
            "ok": False,
            "reason": "empty reply",
            "suggestion": "Produce a substantive reply. Even acknowledgements need context (what was stored, what was logged, what was decided).",
        }

    # Banned-phrase substring scan (case-insensitive)
    lc = stripped.lower()
    for pat, why in BANNED_PATTERNS:
        if re.search(pat, lc, flags=re.IGNORECASE):
            return {
                "ok": False,
                "reason": why,
                "suggestion": "Replace the banned phrase with concrete content — what action you took, what data you fetched, what you stored.",
            }

    # One-word ack scan — only if reply collapses to a single content-free ack.
    # Action-confirming bare words ("done.", "sent.", "moved it.") are the
    # target friend-voice register; they pass.
    bare = re.sub(r"[^\w]", "", lc)
    if bare in EMPTY_ACKS and len(stripped) < 12:
        return {
            "ok": False,
            "reason": f"content-free ack ({bare!r}), looks broken",
            "suggestion": "Bare 'ok' / 'k' / 'ack' don't tie to an action. Either say what you did ('done.', 'moved it to 8:30pm.', 'sent.'), or stay silent if there's nothing to confirm.",
        }

    return {"ok": True, "reply": reply}


# ── CLI ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        print(json.dumps({"ok": True, "skill": "reply-discipline",
                          "user_message": "reply-discipline smoke ok"}))
        return

    candidate = sys.stdin.read()
    print(json.dumps(check(candidate), ensure_ascii=False))


if __name__ == "__main__":
    main()
