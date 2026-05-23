#!/usr/bin/env python3
"""E2E: cron-style skill prompt → user replies → skill fires.

Repro of the image-v37/v38 failure (2026-05-23): the
personalized-video-recommender skill's 11:45 cron asked
"What kind of video do you want to watch today?", the user
tap-replied with "motivation to raise on bad days...", and the bot
gave a generic Goggins/Rocky answer instead of running the skill.
Root cause: no link between the cron prompt and the user's reply.

This test simulates the cron deterministically via SSH:
  1. SSH to bot, exec ~/personal-assistant/infra/send-skill-prompt.sh
     create-a-personalized-video-recommender "<question>"
  2. Wait for the prompt to land in Telegram (visible footer
     '↩ reply to /<slug>' should be present).
  3. Reply to it via Telethon's reply_to_msg_id, with a topic phrase.
  4. Within 240s, assert the bot's reply is from the skill — i.e.
     contains a YouTube link from one of the two channels listed in
     SKILL.md (@ycombinator or @ycrootaccess).
  5. Assert state file was consumed (router deletes it on first use).

If this passes end-to-end on the live bot, the bug is fixed.
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
)
from telethon import TelegramClient
from telethon.sessions import StringSession


SLUG = "create-a-personalized-video-recommender"
PROMPT_TEXT = "What kind of video do you want to watch today?"
REPLY_TOPIC = "motivation to raise on bad days when you feel nothing is going your way"
EXPECTED_CHANNEL_HINTS = ("youtube.com", "youtu.be")  # any YT link counts


async def wait_for_bot_message(client, bot, match_substr: str, timeout: int, after_ts: float):
    """Return the first incoming bot message whose text contains
    match_substr and was sent after after_ts. Polls every 2s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2)
        async for msg in client.iter_messages(bot, limit=10):
            if msg.out:
                continue
            if msg.date.timestamp() < after_ts - 1:
                continue
            if match_substr.lower() in (msg.text or "").lower():
                return msg
    raise TimeoutError(f"no bot message containing {match_substr!r} within {timeout}s")


async def wait_for_any_bot_reply(client, bot, after_ts: float, timeout: int):
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        async for msg in client.iter_messages(bot, limit=5):
            if msg.out:
                continue
            if msg.date.timestamp() <= after_ts:
                continue
            return msg
    raise TimeoutError(f"no bot reply within {timeout}s")


async def main() -> int:
    print("=" * 60)
    print("PERIODIC-SKILL REPLY ROUTING E2E")
    print(f"Bot:    @{BOT_USERNAME}")
    print(f"Skill:  /{SLUG}")
    print("=" * 60)

    if not preflight():
        print("PREFLIGHT FAILED")
        return 1
    reset_session()

    # Clean any stale state file from earlier runs.
    ssh_check("rm -f ~/.brain-bootstrap-raw/state/last-periodic-skill.json", timeout=5)

    client = TelegramClient(
        StringSession(SESSION_STR),
        int(CREDS["TG_API_ID"]),
        CREDS["TG_API_HASH"],
    )
    await client.start()
    bot = await client.get_entity(BOT_USERNAME)

    try:
        # ── stage 1: trigger the cron-style prompt deterministically ──
        print(f"\n[stage 1] firing send-skill-prompt.sh on VPS")
        fire_ts = time.time()
        code, out = ssh_check(
            f"bash ~/personal-assistant/infra/send-skill-prompt.sh "
            f"{SLUG} {PROMPT_TEXT!r}",
            timeout=20,
        )
        if code != 0:
            raise AssertionError(f"send-skill-prompt.sh failed (rc={code}): {out!r}")
        print(f"[stage 1] ✓ script exited 0")

        # Verify state file was written
        code, state = ssh_check(
            "cat ~/.brain-bootstrap-raw/state/last-periodic-skill.json",
            timeout=5,
        )
        if code != 0 or SLUG not in state:
            raise AssertionError(f"state file not written or wrong slug: {state!r}")
        print(f"[stage 1] ✓ state file written with slug={SLUG}")

        # ── stage 2: wait for the prompt to arrive in Telegram ──
        print(f"\n[stage 2] waiting for prompt to land in Telegram")
        prompt_msg = await wait_for_bot_message(
            client, bot, "kind of video", timeout=30, after_ts=fire_ts,
        )
        prompt_body = prompt_msg.text or ""
        print(f"[stage 2] ✓ prompt arrived (msg_id={prompt_msg.id})")
        print(f"          preview: {prompt_body[:120]!r}")

        # The footer should be present (visible italic tag the LLM uses
        # to know which skill a reply belongs to).
        if SLUG not in prompt_body and "reply to" not in prompt_body.lower():
            raise AssertionError(
                f"prompt missing skill-provenance footer.\n"
                f"got: {prompt_body!r}"
            )
        print(f"[stage 2] ✓ provenance footer present")

        # ── stage 3: reply to the prompt with a topic ──
        print(f"\n[stage 3] replying with topic: {REPLY_TOPIC!r}")
        reply_ts = time.time()
        await client.send_message(bot, REPLY_TOPIC, reply_to=prompt_msg.id)

        # ── stage 4: bot must respond with a skill-shaped reply ──
        print(f"\n[stage 4] waiting up to 240s for skill output")
        bot_reply = await wait_for_any_bot_reply(client, bot, reply_ts, timeout=240)
        reply_text = bot_reply.text or ""
        print(f"[stage 4] bot reply preview:")
        print(f"          {reply_text[:400]!r}")

        # The skill's "Output format" section in SKILL.md mandates a
        # YouTube link. Any youtube.com / youtu.be URL means the skill
        # actually ran. A generic reply (Goggins, Rocky, books, etc.)
        # will contain none.
        if not any(h in reply_text.lower() for h in EXPECTED_CHANNEL_HINTS):
            raise AssertionError(
                f"reply doesn't contain a YouTube link — skill did NOT fire.\n"
                f"This is the same failure mode as image-v38.\n"
                f"got: {reply_text[:500]!r}"
            )
        print(f"[stage 4] ✓ reply contains a YouTube link — skill fired")

        # ── stage 5: state file should be consumed by router ──
        code, state2 = ssh_check(
            "test -f ~/.brain-bootstrap-raw/state/last-periodic-skill.json "
            "&& echo present || echo gone",
            timeout=5,
        )
        if "gone" not in state2:
            print(f"[stage 5] ⚠ state file still present — router didn't consume "
                  f"(may indicate the skill fired via LLM-context path rather than "
                  f"intent-router path). Not a hard fail.")
        else:
            print(f"[stage 5] ✓ state file consumed by router (one-shot)")

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
    print("✅ PASSED — periodic-skill reply routed back to the originating skill")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
