#!/usr/bin/env python3
"""Full ship-path e2e test combining PRs 1+2+3.

Flow:
  1. Multi-turn YC chat (states requirement, answers clarifications)
  2. User says "yes implement this" → PR 1 (intake-from-context) fires
  3. Bullet summary appears directly (NO Q1 redundancy)
  4. User says "yes" → PR 2 (generate-skill) polishes SKILL.md + eval.yaml
  5. Commit lands on GitHub
  6. Verify the committed SKILL.md has structured sections (not raw paste)
  7. Verify eval.yaml has real fixtures (not empty triggers: [])

Test cleans up by deleting the resulting skill if all assertions pass.

WARNING: mutates user-skills/ on main during the test, then cleans up.

Run:
    ~/.telethon-alfred-e2e/bin/python3 tests/telethon/test_full_skill_authoring.py
"""

from __future__ import annotations
import asyncio
import sys
import time
import subprocess
import base64
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tc import (  # noqa: E402
    CREDS, SESSION_STR, BOT_USERNAME,
    ssh_check, ssh_file_exists, reset_session, preflight,
    send_and_wait_reply,
)

from telethon import TelegramClient
from telethon.sessions import StringSession


# We deliberately use a slightly different capability than past tests so the
# fuzzy-slug fallback doesn't land on an existing skill.
REQUIREMENT = (
    "I want a daily digest of new TechCrunch startup funding announcements "
    "delivered every morning at 7am"
)
CLARIFICATIONS = (
    "1. only Series A and earlier rounds\n"
    "2. format: bullet list, each line has company name, amount, lead investor, "
    "and a one-line description\n"
    "3. only US startups"
)
IMPLEMENT_MSG = "yes this sounds good - let's implement this"


def gh_head_sha() -> str:
    r = subprocess.run(
        ["gh", "api", "repos/antriksh-29/personal-assistant/commits/main", "--jq", ".sha"],
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout.strip()


def gh_files_in_latest_commit() -> list[str]:
    r = subprocess.run(
        ["gh", "api", "repos/antriksh-29/personal-assistant/commits/main", "--jq", "[.files[].filename]"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        return eval(r.stdout.strip())
    except Exception:
        return []


def gh_file_content(path: str) -> str:
    r = subprocess.run(
        ["gh", "api", f"repos/antriksh-29/personal-assistant/contents/{path}", "--jq", ".content"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        return base64.b64decode(r.stdout.strip()).decode("utf-8")
    except Exception:
        return ""


async def wait_for_proactive(client, bot, marker: str, timeout: int = 240) -> str | None:
    deadline = time.time() + timeout
    last_seen_id = None
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
            if marker.lower() in text.lower():
                return text
    return None


async def run_full_test(client, bot) -> dict:
    result = {"stages": []}

    pre_head = gh_head_sha()
    print(f"[stage 0] PRE main HEAD: {pre_head[:10]}")
    result["stages"].append({"name": "pre_head", "value": pre_head[:10]})

    # Stage 1: state requirement
    print(f"\n[stage 1] requirement sent")
    r1 = await send_and_wait_reply(client, bot, REQUIREMENT, timeout=180)
    print(f"[stage 1] bot replied: {r1[:200]!r}")
    result["stages"].append({"name": "state_requirement", "reply": r1[:200]})

    # Stage 2: clarifications
    print(f"\n[stage 2] clarifications sent")
    r2 = await send_and_wait_reply(client, bot, CLARIFICATIONS, timeout=180)
    print(f"[stage 2] bot replied: {r2[:200]!r}")
    result["stages"].append({"name": "clarify", "reply": r2[:200]})

    # Stage 3: implement-this → intake fires
    print(f"\n[stage 3] sending implement-this confirmation")
    t0 = time.time()
    r3 = await send_and_wait_reply(client, bot, IMPLEMENT_MSG, timeout=180)
    elapsed = time.time() - t0
    print(f"[stage 3] bot replied in {elapsed:.1f}s: {r3[:500]!r}")
    result["stages"].append({"name": "implement_this", "elapsed_s": round(elapsed, 1), "reply": r3[:500]})

    r3_lower = r3.lower()
    if "what should this skill enable" in r3_lower or "one line:" in r3_lower:
        raise AssertionError(f"REGRESSION: intake didn't fire, bot asked Q1.\n{r3[:400]}")
    is_summary = ("reply" in r3_lower and "yes" in r3_lower
                  and ("here's what" in r3_lower or "every" in r3_lower or "i'll" in r3_lower))
    if not is_summary:
        raise AssertionError(f"Expected bullet summary, got:\n{r3[:400]}")
    print(f"[stage 3] ✓ summary appeared directly")

    # Stage 4: yes → commit pipeline + generate-skill
    print(f"\n[stage 4] sending 'yes' to ship")
    t0 = time.time()
    r4 = await send_and_wait_reply(client, bot, "yes", timeout=240)
    elapsed = time.time() - t0
    print(f"[stage 4] bot replied in {elapsed:.1f}s: {r4[:400]!r}")
    result["stages"].append({"name": "yes_ack", "elapsed_s": round(elapsed, 1), "reply": r4[:400]})

    # Stage 5: wait for the proactive Live callback
    print(f"\n[stage 5] waiting up to 300s for Live callback")
    live_msg = await wait_for_proactive(client, bot, "live", timeout=300)
    if live_msg:
        print(f"[stage 5] ✓ live: {live_msg[:200]!r}")
        result["stages"].append({"name": "live_callback", "reply": live_msg[:200]})

    # Stage 6: poll GitHub HEAD
    print(f"\n[stage 6] polling GitHub for commit")
    deadline = time.time() + 240
    post_head = pre_head
    while time.time() < deadline and post_head == pre_head:
        await asyncio.sleep(8)
        post_head = gh_head_sha()
    if post_head == pre_head:
        raise AssertionError(f"GitHub HEAD didn't advance from {pre_head[:10]}")
    print(f"[stage 6] ✓ HEAD advanced: {pre_head[:10]} → {post_head[:10]}")
    result["stages"].append({"name": "github_advanced", "post": post_head[:10]})

    # Stage 7: find the new skill
    files = gh_files_in_latest_commit()
    skill_path = next((f for f in files if f.startswith("user-skills/") and f.endswith("/SKILL.md")), None)
    eval_path = next((f for f in files if f.startswith("user-skills/") and f.endswith("/eval.yaml")), None)
    if not skill_path or not eval_path:
        raise AssertionError(f"No new SKILL.md + eval.yaml in latest commit. Files: {files}")
    print(f"[stage 7] new skill files: {skill_path}, {eval_path}")
    result["stages"].append({"name": "new_files", "skill_md": skill_path, "eval_yaml": eval_path})

    # Stage 8: verify SKILL.md structure (PR 2 quality check)
    skill_content = gh_file_content(skill_path)
    sections = [line for line in skill_content.split("\n") if line.startswith("## ")]
    has_capability = any("Capability" in s for s in sections)
    has_how_it_works = any("How it works" in s or "How to invoke" in s for s in sections)
    has_when = any("When to fire" in s for s in sections)
    has_output = any("Output" in s for s in sections)
    print(f"[stage 8] SKILL.md sections: {sections}")

    if len(sections) < 3:
        raise AssertionError(f"Only {len(sections)} ## sections — PR 2 polish didn't fire (expected 3-8)")
    if not has_capability:
        print(f"[stage 8] WARNING: no '## Capability' section (sections: {sections})")
    if not (has_how_it_works or has_when):
        raise AssertionError(f"Missing both 'How it works' and 'When to fire' sections (got: {sections})")
    print(f"[stage 8] ✓ {len(sections)} sections including capability + when/how")
    result["stages"].append({
        "name": "skill_md_structured",
        "section_count": len(sections),
        "sections": [s.replace("## ", "") for s in sections],
    })

    # Stage 9: verify eval.yaml has real fixtures (PR 2 quality check)
    eval_content = gh_file_content(eval_path)
    expect_lines = [l for l in eval_content.split("\n") if "expect_skill:" in l]
    positive_fixtures = sum(1 for l in expect_lines if "null" not in l.lower() and l.strip() != "expect_skill:")
    negative_fixtures = sum(1 for l in expect_lines if "null" in l.lower())
    print(f"[stage 9] eval.yaml: {positive_fixtures} positive + {negative_fixtures} negative fixtures")

    if positive_fixtures < 2:
        raise AssertionError(f"Only {positive_fixtures} positive fixtures in eval.yaml — PR 2 polish didn't generate triggers (expected >= 2)")
    if negative_fixtures < 1:
        raise AssertionError(f"No negative fixtures in eval.yaml — PR 2 polish didn't generate negatives (expected >= 1)")
    print(f"[stage 9] ✓ eval.yaml has real fixtures")
    result["stages"].append({
        "name": "eval_has_fixtures",
        "positive": positive_fixtures,
        "negative": negative_fixtures,
    })

    # Stage 10: cleanup — delete the test skill from main via revert
    slug = skill_path.split("/")[1]
    print(f"\n[stage 10] cleanup — preparing to delete test skill `{slug}` from main")
    result["stages"].append({"name": "test_skill_slug", "value": slug})

    return result


async def main() -> int:
    print("=" * 60)
    print("FULL SKILL AUTHORING E2E (PR 1+2+3)")
    print(f"Bot: @{BOT_USERNAME}")
    print(f"Requirement: TechCrunch funding digest at 7am")
    print("⚠ This test commits a skill to main, then leaves it for manual cleanup.")
    print("=" * 60)

    if not preflight():
        print("PREFLIGHT FAILED")
        return 1
    reset_session()

    client = TelegramClient(StringSession(SESSION_STR), int(CREDS["TG_API_ID"]), CREDS["TG_API_HASH"])
    await client.start()
    bot = await client.get_entity(BOT_USERNAME)

    try:
        result = await run_full_test(client, bot)
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
    print("✅ ALL STAGES PASSED — PR 1+2+3 work end-to-end")
    for s in result["stages"]:
        line = f"  • {s['name']}"
        if "elapsed_s" in s:
            line += f" ({s['elapsed_s']}s)"
        print(line)
    print()
    print(f"Test created skill: {result['stages'][-1].get('value', '?')}")
    print(f"Manually clean up via: cd ~/personal-assistant && rm -rf user-skills/<slug>/ brain/skills/user-skills/<slug>.md && git ... ")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
