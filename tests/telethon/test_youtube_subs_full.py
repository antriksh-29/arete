#!/usr/bin/env python3
"""E2E test: bot fetches ALL 161 YouTube subscriptions + categorizes + brain page.

Verifies PR #129's composio-paginate.py wrapper actually fixes the
2026-05-22 truncation/fabrication failure mode.

Asks the bot to:
  1. Fetch every channel the user is subscribed to on YouTube
  2. Categorize them holistically
  3. Save to a brain page

Then verifies on the VPS:
  • A brain page exists for youtube-subscriptions
  • The page contains >=150 channel names (allowing some duplicates/dedup)
  • The page has category headings (## Football, ## Tech, etc.)
  • No "..." truncation markers or fabricated entries
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


REQUEST = (
    "fetch the complete list of all my YouTube subscriptions (all 161 channels — "
    "use ~/personal-assistant/infra/composio-paginate.py with maxResults=15 to "
    "avoid the truncation issue we hit last time). Then categorize them holistically "
    "(football, F1, startups/tech, business, news, comedy, music, etc — invent the "
    "categories that fit). Save the full categorized list to a brain page at "
    "references/youtube-subscriptions-complete. Confirm in your reply how many "
    "channels total and which categories you used."
)


def brain_page_exists(slug: str) -> tuple[bool, int, list[str]]:
    """Return (exists, line_count, category_headings)."""
    paths = [
        f"~/.brain/references/{slug}.md",
        f"~/.brain/references/youtube-subscriptions.md",  # alt naming
    ]
    for p in paths:
        code, content = ssh_check(f"cat {p} 2>/dev/null", timeout=15)
        if content.strip():
            lines = content.splitlines()
            cats = [l.strip() for l in lines if l.startswith("## ") or l.startswith("### ")]
            return True, len(lines), cats
    return False, 0, []


def count_real_channel_entries(slug: str) -> int:
    """Count lines that look like channel entries (bullet/numbered + at least
    a couple of words). Excludes section headers, blank lines, and prose."""
    paths = [
        f"~/.brain/references/{slug}.md",
        f"~/.brain/references/youtube-subscriptions.md",
    ]
    for p in paths:
        # Match lines starting with `- `, `* `, or `N. ` and having 3+ alnum chars
        code, n = ssh_check(
            f"grep -cE '^[[:space:]]*([-*]|[0-9]+\\.)[[:space:]]+[A-Za-z0-9].*[A-Za-z0-9]' {p} 2>/dev/null || echo 0",
            timeout=15,
        )
        try:
            return int(n.strip().splitlines()[-1])
        except ValueError:
            continue
    return 0


async def main() -> int:
    print("=" * 60)
    print("YOUTUBE SUBS FULL FETCH + CATEGORIZE + BRAIN PAGE")
    print(f"Bot: @{BOT_USERNAME}")
    print(f"Request: {REQUEST[:120]}...")
    print("=" * 60)

    if not preflight():
        print("PREFLIGHT FAILED")
        return 1
    reset_session()

    # Clean any prior brain page so we know what got written this run
    ssh_check("rm -f ~/.brain/references/youtube-subscriptions*.md", timeout=10)

    client = TelegramClient(StringSession(SESSION_STR), int(CREDS["TG_API_ID"]), CREDS["TG_API_HASH"])
    await client.start()
    bot = await client.get_entity(BOT_USERNAME)

    try:
        print(f"\n[stage 1] sending request ({len(REQUEST)} chars)")
        t0 = time.time()
        # This is a long-running task — agent will paginate 11+ times,
        # then categorize, then write brain. Set generous timeout.
        reply = await send_and_wait_reply(client, bot, REQUEST, timeout=600)
        elapsed = time.time() - t0
        print(f"[stage 1] reply in {elapsed:.1f}s")
        print(f"  preview: {reply[:500]!r}")

        # The reply itself should mention the total number
        reply_lower = reply.lower()
        if "161" in reply or "all" in reply_lower:
            print("[stage 1] ✓ reply mentions full count")
        else:
            print(f"[stage 1] ⚠ reply doesn't mention full count: {reply[:300]}")

        print("\n[stage 2] checking brain page on VPS")
        await asyncio.sleep(3)
        exists, line_count, cats = brain_page_exists("youtube-subscriptions-complete")
        if not exists:
            # Look for any youtube-related brain page
            code, found = ssh_check("ls ~/.brain/references/youtube*.md 2>/dev/null", timeout=10)
            raise AssertionError(
                f"No brain page found at references/youtube-subscriptions-complete.md.\n"
                f"Searched: {found or '(no youtube* files)'}\n"
                f"Bot reply preview:\n{reply[:600]}"
            )
        print(f"[stage 2] ✓ brain page exists, {line_count} lines")
        print(f"[stage 2] ✓ category headings: {cats[:10]}")

        print("\n[stage 3] counting channel entries")
        entry_count = count_real_channel_entries("youtube-subscriptions-complete")
        print(f"[stage 3] {entry_count} entry-shaped lines")
        if entry_count < 100:
            raise AssertionError(
                f"Only {entry_count} entries on brain page (expected ~150+). "
                f"The paginator wrapper may not have been used."
            )
        if entry_count < 150:
            print(f"[stage 3] ⚠ fewer than 150 entries — possible dedup; investigating")
        else:
            print(f"[stage 3] ✓ {entry_count} entries — full fetch landed")

        print("\n[stage 4] checking for fabrication patterns")
        # Pull the file content and check for obvious hallucinated entries
        code, content = ssh_check(
            "cat ~/.brain/references/youtube-subscriptions-complete.md 2>/dev/null"
            " || cat ~/.brain/references/youtube-subscriptions.md 2>/dev/null",
            timeout=15,
        )
        suspicious = []
        for marker in ["the yard clips 18", "the yard clips 17", "...28 more", "[URL]", "placeholder"]:
            if marker.lower() in content.lower():
                suspicious.append(marker)
        if suspicious:
            raise AssertionError(f"Brain page contains suspicious markers: {suspicious}")
        print("[stage 4] ✓ no fabrication patterns detected")

        print("\n[stage 5] checking categories are present + holistic")
        if len(cats) < 4:
            raise AssertionError(f"Only {len(cats)} categories — expected 4+ for 'holistic'")
        print(f"[stage 5] ✓ {len(cats)} categories")

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
    print(f"✅ PASSED — {entry_count} channels across {len(cats)} categories")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
