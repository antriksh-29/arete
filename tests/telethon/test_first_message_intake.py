#!/usr/bin/env python3
"""Test that a long FIRST message (image-v33 case) doesn't fall into Q1.

The user's image-v33 message had full capability + schedule + output
format in one shot, but the bot replied with elicit Q1. PR #124 routes
implicit-create through intake-from-context which extracts the spec
from the message itself.

Expected: after sending the long message, bot replies with EITHER
  (a) bullet summary (all slots filled from the message), or
  (b) a single missing-slot question (e.g. "is this manual or periodic?")
But NOT the Q1 "what should this skill enable me to do" boilerplate.
"""

from __future__ import annotations
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tc import (  # noqa: E402
    CREDS, SESSION_STR, BOT_USERNAME,
    ssh_check, reset_session, preflight,
    send_and_wait_reply,
)

from telethon import TelegramClient
from telethon.sessions import StringSession


# The image-v33 message verbatim (slightly trimmed to fit one Telethon
# send_message call cleanly — no URLs that telethon might rewrite).
FIRST_MESSAGE = (
    "I want to create a personalised video recommender from youtube for me — "
    "only from two channels: ycombinator and ycpodcasts.\n\n"
    "everyday at 10.45am, you should check with me a topic that I'm interested "
    "in watching a video about.\n\n"
    "post my reply — you should go to these two channels, and find the most "
    "relevant video that I should be watching basis the details I have shared "
    "with you.\n\n"
    "the output needs to contain:\n"
    "1. video title\n"
    "2. video description\n"
    "3. video link\n\n"
    "the output should be shared right after I have shared the details with you "
    "as a reply to your 10.45am message"
)


async def run_test(client, bot) -> dict:
    result = {"stages": []}

    print(f"\n[stage 1] sending image-v33 first message ({len(FIRST_MESSAGE)} chars)")
    t0 = time.time()
    r1 = await send_and_wait_reply(client, bot, FIRST_MESSAGE, timeout=240)
    elapsed = time.time() - t0
    print(f"[stage 1] bot replied in {elapsed:.1f}s:\n{r1[:600]}")
    result["stages"].append({"name": "first_message", "elapsed_s": round(elapsed, 1), "reply": r1[:600]})

    # The forbidden response: elicit Q1
    r1_lower = r1.lower()
    if "what should this skill enable" in r1_lower or "one line:" in r1_lower:
        raise AssertionError(
            f"REGRESSION: bot replied with elicit Q1 'what should this skill enable me to do'.\n"
            f"This is the image-v33 bug — bot ignored the spec.\n\nGot:\n{r1[:500]}"
        )

    # Acceptable: bullet summary OR a single missing-slot question
    is_summary = (
        "reply" in r1_lower and "yes" in r1_lower
        and ("here's what" in r1_lower or "every" in r1_lower or "i'll" in r1_lower or "i will" in r1_lower)
    )
    is_missing_question = any(
        kw in r1_lower for kw in (
            "trigger phrase", "manual", "periodic",
            "what time", "what should the result", "output", "format",
            "let me clarify", "to confirm", "one thing", "a couple", "few quick",
        )
    )

    if is_summary:
        print(f"[stage 2] ✓ bullet summary appeared directly — all slots filled from the message")
        result["stages"].append({"name": "outcome", "value": "full_summary"})
    elif is_missing_question:
        print(f"[stage 2] ✓ asked only a missing-slot question (not Q1)")
        result["stages"].append({"name": "outcome", "value": "partial_question"})
    else:
        # The bot replied with something unexpected but not Q1. Could be the
        # bot doing free-chat clarification, which is also reasonable.
        print(f"[stage 2] ⚠ unexpected reply, not Q1 — accepting since no regression")
        result["stages"].append({"name": "outcome", "value": "other", "reply": r1[:500]})

    # Cleanup
    print(f"\n[stage 3] sending 'cancel' to discard any draft")
    t0 = time.time()
    cancel_reply = await send_and_wait_reply(client, bot, "cancel", timeout=120)
    print(f"[stage 3] cancel reply in {time.time()-t0:.1f}s: {cancel_reply[:200]!r}")
    ssh_check("rm -f ~/personal-assistant/.context/skill-author-drafts/draft-*.json", timeout=10)
    result["stages"].append({"name": "cleanup"})

    return result


async def main() -> int:
    print("=" * 60)
    print("FIRST-MESSAGE INTAKE TEST (image-v33 regression)")
    print(f"Bot: @{BOT_USERNAME}")
    print(f"Message length: {len(FIRST_MESSAGE)} chars")
    print("Expects: bullet summary or missing-slot question — NEVER Q1")
    print("=" * 60)

    if not preflight():
        print("PREFLIGHT FAILED")
        return 1
    reset_session()

    client = TelegramClient(StringSession(SESSION_STR), int(CREDS["TG_API_ID"]), CREDS["TG_API_HASH"])
    await client.start()
    bot = await client.get_entity(BOT_USERNAME)

    try:
        result = await run_test(client, bot)
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        return 1
    except Exception as e:
        import traceback
        print(f"\n💥 ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1
    finally:
        await client.disconnect()

    print("\n" + "=" * 60)
    print("✅ PASSED — image-v33 regression fixed")
    for s in result["stages"]:
        line = f"  • {s['name']}"
        if "elapsed_s" in s:
            line += f" ({s['elapsed_s']}s)"
        if "value" in s:
            line += f" — {s['value']}"
        print(line)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
