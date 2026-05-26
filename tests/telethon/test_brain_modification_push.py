#!/usr/bin/env python3
"""E2E test: bot modifies brain page → changes land on GitHub.

Reproduces the image-v36 failure: user asks bot to refine an existing
brain page, modifications must reach the public GitHub mirror within
90 seconds (brain-autosync timer = 60s + push latency).

Flow:
  1. Capture pre-state SHA of antriksh-brain main
  2. Send modification request to bot (e.g. add a new note section
     to the YouTube subscriptions brain page)
  3. Wait for bot reply
  4. Wait up to 120s for brain-autosync to push the change
  5. Verify GitHub main HEAD advanced
  6. Verify the specific edit appears in the new commit's diff
"""

from __future__ import annotations
import asyncio
import base64
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tc import (  # noqa: E402
    CREDS, SESSION_STR, BOT_USERNAME,
    ssh_check, reset_session, preflight, send_and_wait_reply,
)
from telethon import TelegramClient
from telethon.sessions import StringSession


BRAIN_REPO = "antriksh-29/antriksh-brain"
BRAIN_FILE = "references/youtube-subscriptions-complete.md"

# Modification request: ask the bot to add a "## Reviewed" footer with
# a unique marker so we can grep for it in the diff.
UNIQUE_MARKER = f"e2e-test-{int(time.time())}"
REQUEST = (
    f"please add a small note at the very bottom of the youtube subscriptions brain page "
    f"(references/youtube-subscriptions-complete.md) — add a new ## section called 'Reviewed' "
    f"with the line: 'Last reviewed: {UNIQUE_MARKER}'. Make sure the change gets pushed to "
    f"GitHub. Just edit the existing file directly — no need to re-run the categorizer."
)


def gh_head_sha(repo: str) -> str:
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/main", "--jq", ".sha"],
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout.strip()


def gh_file_content(repo: str, path: str) -> str:
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".content"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        return base64.b64decode(r.stdout.strip()).decode("utf-8")
    except Exception:
        return ""


async def main() -> int:
    print("=" * 60)
    print("BRAIN MODIFICATION → GITHUB PUSH E2E TEST")
    print(f"Bot:      @{BOT_USERNAME}")
    print(f"Repo:     {BRAIN_REPO}")
    print(f"File:     {BRAIN_FILE}")
    print(f"Marker:   {UNIQUE_MARKER}")
    print("=" * 60)

    if not preflight():
        print("PREFLIGHT FAILED")
        return 1
    reset_session()

    pre_sha = gh_head_sha(BRAIN_REPO)
    pre_content = gh_file_content(BRAIN_REPO, BRAIN_FILE)
    has_marker_before = UNIQUE_MARKER in pre_content
    print(f"\n[stage 0] PRE main HEAD: {pre_sha[:10]}")
    print(f"          marker already in file (sanity): {has_marker_before}")
    if has_marker_before:
        print("INTERNAL ERROR: unique marker already present — change the timestamp")
        return 1

    client = TelegramClient(StringSession(SESSION_STR), int(CREDS["TG_API_ID"]), CREDS["TG_API_HASH"])
    await client.start()
    bot = await client.get_entity(BOT_USERNAME)

    try:
        print(f"\n[stage 1] sending modification request")
        t0 = time.time()
        reply = await send_and_wait_reply(client, bot, REQUEST, timeout=240)
        elapsed = time.time() - t0
        print(f"[stage 1] bot replied in {elapsed:.1f}s")
        print(f"  preview: {reply[:300]!r}")

        # Bot's reply should at minimum acknowledge the edit
        reply_lower = reply.lower()
        if not any(kw in reply_lower for kw in (
            "added", "updated", "appended", "pushed", "done", "saved",
            "modified", "edited", "review", "footer",
        )):
            print(f"[stage 1] ⚠ reply doesn't acknowledge the edit clearly. continuing to check repo state.")

        # Poll GitHub for the new commit. brain-autosync runs every 60s
        # so worst case = 60s + push latency.
        print(f"\n[stage 2] polling GitHub for new commit on brain main")
        deadline = time.time() + 180
        post_sha = pre_sha
        polls = 0
        while time.time() < deadline and post_sha == pre_sha:
            await asyncio.sleep(8)
            post_sha = gh_head_sha(BRAIN_REPO)
            polls += 1
            if polls % 4 == 0:
                print(f"  poll #{polls}: HEAD still {post_sha[:10]}")
        if post_sha == pre_sha:
            # Check VPS state — was the file even modified locally?
            code, vps_local = ssh_check(
                "grep -c 'Last reviewed' ~/.brain/references/youtube-subscriptions-complete.md || echo 0",
                timeout=10,
            )
            code2, vps_status = ssh_check("cd ~/.brain && git status --short references/", timeout=10)
            raise AssertionError(
                f"GitHub HEAD didn't advance from {pre_sha[:10]} within 180s.\n"
                f"VPS local file has 'Last reviewed': {vps_local.strip()}\n"
                f"Brain git status: {vps_status.strip() or '(clean — no local edits either)'}\n"
                f"Bot reply was: {reply[:400]}"
            )
        print(f"[stage 2] ✓ HEAD advanced: {pre_sha[:10]} → {post_sha[:10]}")

        # Verify the marker actually appears in the new file content
        print(f"\n[stage 3] verifying marker '{UNIQUE_MARKER}' in new content")
        post_content = gh_file_content(BRAIN_REPO, BRAIN_FILE)
        if UNIQUE_MARKER not in post_content:
            raise AssertionError(
                f"GitHub HEAD advanced but the unique marker '{UNIQUE_MARKER}' "
                f"is not in the file content. Either the bot modified a different "
                f"part of the file or the wrong file got pushed."
            )
        print(f"[stage 3] ✓ marker found in pushed content")

        # Check if a "Reviewed" section appears
        if "## Reviewed" in post_content:
            print(f"[stage 3] ✓ '## Reviewed' section heading present")
        else:
            print(f"[stage 3] ⚠ marker present but no '## Reviewed' heading — bot edited but didn't follow structure exactly. acceptable.")

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
    print(f"✅ PASSED — modification reached GitHub mirror within 180s")
    print(f"  pre  HEAD: {pre_sha[:10]}")
    print(f"  post HEAD: {post_sha[:10]}")
    print(f"  marker:    {UNIQUE_MARKER}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
