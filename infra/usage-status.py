#!/usr/bin/env python3
# usage-status — today's spend / token usage report (placeholder).
#
# Wave 4A wired the SKILL.md, eval, and resolver entry but left the
# real cost-log read as a TODO: the OpenClaw cost-log location on the
# VPS isn't documented in this repo yet. Once that's known, replace
# the placeholder body with a real read of the log.
#
# Usage:
#   usage-status.py             → today's spend (placeholder)
#   usage-status.py yesterday   → yesterday's spend (placeholder)
#   usage-status.py --smoke

import json
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        print(json.dumps({"ok": True, "skill": "usage-status",
                          "user_message": "usage-status smoke ok"}))
        return

    when = sys.argv[1] if len(sys.argv) > 1 else "today"

    # TODO(wave-4b): replace with a real read of OpenClaw's cost log.
    # Probable paths to check on the VPS:
    #   ~/.openclaw/logs/usage.jsonl
    #   ~/.openclaw/cost.log
    #   journalctl --user -u openclaw-gateway | grep cost
    # Until then, surface the limitation honestly — don't fake a number.
    print(json.dumps({
        "ok": True,
        "skill": "usage-status",
        "scope": when,
        "user_message": (
            f"Usage report for {when} not yet wired — the OpenClaw cost-log "
            "path needs to be located on the VPS before this returns real "
            "numbers. Tracking in Wave 4B. Use `~/bin/bot-cred get OPENAI_API_KEY` "
            "+ provider dashboard for now."
        ),
    }))


if __name__ == "__main__":
    main()
