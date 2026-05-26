#!/usr/bin/env python3
"""Full ship-path e2e test for refine-diff (yes → commit → GitHub).

WARNING: This test mutates the live paul-graham-insights skill on main.
Run only when you intend to actually ship the change.

Pipeline verified:
  1. capture GitHub HEAD + current SKILL.md sha
  2. reset session
  3. send refine request → expect bullet summary
  4. send "yes" → expect "Committed ... pushed" ack
  5. wait for "🎉 Live" callback (≤180s) — OR poll GitHub for the new commit
  6. verify a new commit landed on main touching user-skills/paul-graham-insights/SKILL.md
  7. verify the new SKILL.md content differs from the pre-state
  8. verify the LLM's intended change (Source URL inclusion) is present somewhere
"""

from __future__ import annotations
import asyncio
import sys
import time
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tc import (  # noqa: E402
    CREDS, SESSION_STR, BOT_USERNAME,
    ssh_check, ssh_file_exists, reset_session, preflight,
    send_and_wait_reply,
)

from telethon import TelegramClient
from telethon.sessions import StringSession


SLUG = "paul-graham-insights"
REFINE_REQUEST = (
    "can you please make paul-graham-insights also include the source URL "
    "for each insight going forward"
)


def gh_head_sha() -> str:
    r = subprocess.run(
        ["gh", "api", "repos/antriksh-29/personal-assistant/commits/main", "--jq", ".sha"],
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout.strip()


def gh_file_sha(path: str) -> str:
    """Return the blob sha for a file on origin/main."""
    r = subprocess.run(
        ["gh", "api", f"repos/antriksh-29/personal-assistant/contents/{path}", "--jq", ".sha"],
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout.strip()


def gh_file_content(path: str) -> str:
    r = subprocess.run(
        ["gh", "api", f"repos/antriksh-29/personal-assistant/contents/{path}", "--jq", ".content"],
        capture_output=True, text=True, timeout=15,
    )
    import base64
    try:
        return base64.b64decode(r.stdout.strip()).decode("utf-8")
    except Exception:
        return ""


async def wait_for_proactive(client, bot, marker_substring: str, timeout: int = 240) -> str | None:
    """Poll for a proactive bot message containing the marker. Returns
    the matching message text or None on timeout. Distinct from
    send_and_wait_reply: we don't send anything, just watch."""
    deadline = time.time() + timeout
    last_seen_id = None
    # Note the last message id at start so we only catch new ones
    async for msg in client.iter_messages(bot, limit=1):
        last_seen_id = msg.id
        break
    while time.time() < deadline:
        await asyncio.sleep(4)
        async for msg in client.iter_messages(bot, limit=10):
            if msg.id == last_seen_id:
                break
            if msg.out:
                continue
            text = msg.text or ""
            if marker_substring.lower() in text.lower():
                return text
        # don't advance last_seen_id — we want to catch any matching new msg
    return None


async def run_ship_test(client, bot) -> dict:
    result = {"stages": []}

    # --- Stage 0: capture pre-state ----------------------------------
    pre_head = gh_head_sha()
    pre_blob = gh_file_sha(f"user-skills/{SLUG}/SKILL.md")
    pre_content = gh_file_content(f"user-skills/{SLUG}/SKILL.md")
    pre_url_count = pre_content.count("Source:") + pre_content.count("[Source](") + pre_content.count("paulgraham.com")
    print(f"[stage 0] PRE-STATE:")
    print(f"  main HEAD: {pre_head[:10]}")
    print(f"  SKILL.md blob sha: {pre_blob[:10]}")
    print(f"  SKILL.md size: {len(pre_content)}b")
    print(f"  source-URL markers in body: {pre_url_count}")
    result["stages"].append({
        "name": "pre_state",
        "head": pre_head[:10],
        "blob": pre_blob[:10],
        "skill_md_size": len(pre_content),
        "url_markers": pre_url_count,
    })

    # --- Stage 1: send the refine request ----------------------------
    print(f"\n[stage 1] sending refine request")
    t0 = time.time()
    summary_reply = await send_and_wait_reply(client, bot, REFINE_REQUEST, timeout=180)
    elapsed = time.time() - t0
    print(f"[stage 1] summary in {elapsed:.1f}s: {summary_reply[:400]!r}")
    if "diff looked off" in summary_reply.lower():
        raise AssertionError(f"refine-diff fell back: {summary_reply}")
    if "reply" not in summary_reply.lower() or "yes" not in summary_reply.lower():
        raise AssertionError(f"reply does not look like a summary envelope: {summary_reply}")
    result["stages"].append({"name": "summary", "elapsed_s": round(elapsed, 1), "reply": summary_reply[:500]})

    # --- Stage 2: send yes -------------------------------------------
    print(f"\n[stage 2] sending 'yes'")
    t0 = time.time()
    yes_reply = await send_and_wait_reply(client, bot, "yes", timeout=180)
    elapsed = time.time() - t0
    print(f"[stage 2] yes ack in {elapsed:.1f}s: {yes_reply[:400]!r}")
    # Accept any of: "Committed", "📦", "pushed", "creating", "deploying"
    ack_markers = ["committed", "📦", "pushed", "creating", "deploying", "wait for", "1-2 min", "live"]
    if not any(m in yes_reply.lower() for m in ack_markers):
        raise AssertionError(f"yes ack missing expected wording: {yes_reply}")
    result["stages"].append({"name": "yes_ack", "elapsed_s": round(elapsed, 1), "reply": yes_reply[:500]})

    # --- Stage 3: wait for the proactive 🎉 Live callback -----------
    # commit-and-deploy pushes to main, GH Actions runs, then the
    # deploy hook on the VPS sends the proactive "Live" message.
    print(f"\n[stage 3] waiting up to 240s for proactive 'Live' callback")
    live_msg = await wait_for_proactive(client, bot, "live", timeout=240)
    if live_msg:
        print(f"[stage 3] ✓ live callback: {live_msg[:300]!r}")
        result["stages"].append({"name": "live_callback", "reply": live_msg[:500]})
    else:
        print(f"[stage 3] ⚠ no proactive 'Live' callback within 240s — checking GitHub directly")
        result["stages"].append({"name": "live_callback", "reply": None, "verdict": "timeout"})

    # --- Stage 4: poll GitHub for the new commit ---------------------
    print(f"\n[stage 4] polling GitHub for a new commit on main")
    deadline = time.time() + 180
    post_head = pre_head
    while time.time() < deadline and post_head == pre_head:
        await asyncio.sleep(8)
        post_head = gh_head_sha()
    if post_head == pre_head:
        raise AssertionError(f"GitHub main HEAD did not advance from {pre_head[:10]} within 180s")
    print(f"[stage 4] ✓ HEAD advanced: {pre_head[:10]} → {post_head[:10]}")
    result["stages"].append({"name": "github_head_advanced", "pre": pre_head[:10], "post": post_head[:10]})

    # --- Stage 5: verify SKILL.md actually changed -------------------
    post_blob = gh_file_sha(f"user-skills/{SLUG}/SKILL.md")
    post_content = gh_file_content(f"user-skills/{SLUG}/SKILL.md")
    post_url_count = post_content.count("Source:") + post_content.count("[Source](") + post_content.count("paulgraham.com")
    print(f"[stage 5] POST-STATE:")
    print(f"  SKILL.md blob sha: {post_blob[:10]} (was {pre_blob[:10]})")
    print(f"  SKILL.md size: {len(post_content)}b (was {len(pre_content)}b)")
    print(f"  source-URL markers: {post_url_count} (was {pre_url_count})")
    if post_blob == pre_blob:
        raise AssertionError(f"SKILL.md blob sha unchanged on GitHub — content did not update")
    result["stages"].append({
        "name": "skill_md_changed",
        "pre_blob": pre_blob[:10],
        "post_blob": post_blob[:10],
        "pre_size": len(pre_content),
        "post_size": len(post_content),
        "pre_url_markers": pre_url_count,
        "post_url_markers": post_url_count,
    })

    # --- Stage 6: spot-check the intent landed -----------------------
    # The LLM may have used "[Source](URL)", "Source: URL", a [URL] placeholder,
    # or added a description note. Don't require a perfect format — just
    # verify some "source" or "URL" reference is now present.
    if "source" not in post_content.lower() and "url" not in post_content.lower():
        raise AssertionError("post-content has no 'source'/'URL' mention — change semantically missing")
    print(f"[stage 6] ✓ source/URL mention present in new content")
    result["stages"].append({"name": "semantic_change_present", "verdict": "ok"})

    return result


async def main() -> int:
    print("=" * 60)
    print("REFINE-DIFF FULL SHIP E2E TEST")
    print(f"Bot: @{BOT_USERNAME}")
    print(f"Request: {REFINE_REQUEST!r}")
    print(f"⚠ This test mutates user-skills/{SLUG}/SKILL.md on main.")
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
        result = await run_ship_test(client, bot)
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
    print("✅ ALL STAGES PASSED — refine-diff shipped to GitHub end-to-end")
    for s in result["stages"]:
        line = f"  • {s['name']}"
        if "elapsed_s" in s:
            line += f" ({s['elapsed_s']}s)"
        print(line)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
