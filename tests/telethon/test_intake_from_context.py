#!/usr/bin/env python3
"""End-to-end test for intake-from-context (PR #121).

Simulates the YC daily-report chat flow from the user's screenshots:
  1. User: states the requirement (long natural-language ask)
  2. Bot: asks 1-3 clarifying questions (free chat)
  3. User: answers those clarifications
  4. Bot: proposes a logic + sample output
  5. User: "yes this sounds good - let's implement this"
  6. EXPECT: bullet summary directly (NOT Q1: "what should this skill...")
  7. User: "cancel" — discards draft (avoids polluting prod with test skills)

WARNING: this test is slow (~3-5 minutes) because it goes through 5
genuine LLM turns + 1 intake extraction.

Run:
    ~/.telethon-alfred-e2e/bin/python3 tests/telethon/test_intake_from_context.py
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


async def run_intake_test(client, bot) -> dict:
    result = {"stages": []}

    # --- Stage 1: state the requirement -----------------------------
    requirement = (
        "help me get a daily report on all the top yc company launches/"
        "announcements made by yc itself in the last 24 hours\n\n"
        "deliver this daily to me at 6:15pm"
    )
    print(f"\n[stage 1] sending initial requirement (~140 chars)")
    t0 = time.time()
    r1 = await send_and_wait_reply(client, bot, requirement, timeout=180)
    print(f"[stage 1] bot replied in {time.time()-t0:.1f}s: {r1[:200]!r}")
    result["stages"].append({"name": "state_requirement", "reply": r1[:300]})

    # --- Stage 2: answer clarifications -----------------------------
    clarifications = (
        "1. top here can mean announced by yc on X\n"
        "2. format should be bullet points with one line description "
        "along with the yc post link\n"
        "3. just launches - new companies coming out of the latest batch"
    )
    print(f"\n[stage 2] sending clarifications")
    t0 = time.time()
    r2 = await send_and_wait_reply(client, bot, clarifications, timeout=180)
    print(f"[stage 2] bot replied in {time.time()-t0:.1f}s: {r2[:200]!r}")
    result["stages"].append({"name": "clarify", "reply": r2[:300]})

    # --- Stage 3: confirmation to implement (THIS is what triggers intake) ---
    confirm_msg = "yes this sounds good - let's implement this"
    print(f"\n[stage 3] sending implement-this confirmation: {confirm_msg!r}")
    t0 = time.time()
    r3 = await send_and_wait_reply(client, bot, confirm_msg, timeout=180)
    elapsed = time.time() - t0
    print(f"[stage 3] bot replied in {elapsed:.1f}s: {r3[:500]!r}")
    result["stages"].append({"name": "implement_this", "elapsed_s": round(elapsed, 1), "reply": r3[:500]})

    # --- Stage 4: VERIFY no Q1 redundancy ---------------------------
    r3_lower = r3.lower()
    is_q1 = ("what should this skill enable" in r3_lower
             or "one line:" in r3_lower)
    is_summary = ("reply" in r3_lower and "yes" in r3_lower
                  and ("here's what" in r3_lower
                       or "every day at" in r3_lower
                       or "i'll" in r3_lower))
    is_missing_question = ("trigger phrases" in r3_lower
                           or "manual" in r3_lower and "periodic" in r3_lower
                           or "what time of day" in r3_lower
                           or "what should the result look like" in r3_lower)

    if is_q1:
        raise AssertionError(
            f"REGRESSION: intake-from-context didn't fire — bot asked Q1 from scratch.\n"
            f"Got: {r3[:400]}"
        )
    if not is_summary and not is_missing_question:
        raise AssertionError(
            f"Unexpected bot reply — neither bullet summary nor a missing-question prompt.\n"
            f"Got: {r3[:400]}"
        )
    if is_summary:
        print(f"[stage 4] ✓ intake fired AND filled all slots — got direct bullet summary")
        result["stages"].append({"name": "intake_fired_full", "verdict": "ok"})
    else:
        print(f"[stage 4] ✓ intake fired but had missing slots — got next-question prompt")
        result["stages"].append({"name": "intake_fired_partial", "verdict": "ok"})

    # --- Stage 5: cancel to clean up --------------------------------
    print(f"\n[stage 5] sending 'cancel' to discard draft")
    t0 = time.time()
    cancel_reply = await send_and_wait_reply(client, bot, "cancel", timeout=120)
    print(f"[stage 5] cancel reply in {time.time()-t0:.1f}s: {cancel_reply[:200]!r}")
    result["stages"].append({"name": "cancel", "reply": cancel_reply[:300]})

    # --- Stage 6: verify no draft remains on disk -------------------
    code, out = ssh_check(
        "ls ~/personal-assistant/.context/skill-author-drafts/*.json 2>/dev/null | wc -l",
        timeout=15,
    )
    remaining = int(out.strip() or 0)
    if remaining > 0:
        # Some draft files may persist (cancel doesn't always delete) — force-clean
        ssh_check("rm -f ~/personal-assistant/.context/skill-author-drafts/draft-*.json")
        print(f"[stage 6] cleanup: removed {remaining} leftover draft(s)")
    result["stages"].append({"name": "cleanup", "remaining": remaining})

    return result


async def main() -> int:
    print("=" * 60)
    print("INTAKE-FROM-CONTEXT E2E TEST (PR #121)")
    print(f"Bot: @{BOT_USERNAME}")
    print("Simulates YC daily-report chat → 'yes implement this'")
    print("Expects: bullet summary (NOT Q1 'what should this skill...')")
    print("=" * 60)

    if not preflight():
        print("PREFLIGHT FAILED")
        return 1

    reset_session()

    client = TelegramClient(
        StringSession(SESSION_STR),
        int(CREDS["TG_API_ID"]),
        CREDS["TG_API_HASH"],
    )
    await client.start()
    bot = await client.get_entity(BOT_USERNAME)

    try:
        result = await run_intake_test(client, bot)
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
    print("✅ ALL STAGES PASSED")
    for s in result["stages"]:
        line = f"  • {s['name']}"
        if "elapsed_s" in s:
            line += f" ({s['elapsed_s']}s)"
        if "verdict" in s:
            line += f" — {s['verdict']}"
        print(line)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
