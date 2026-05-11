#!/usr/bin/env python3
# e2e-harness.py — drives @alfred_antriksh_bot via Telethon as your
# user account, runs the test cases in infra/e2e-tests.yaml, and
# reports pass/fail with per-test rationale.
#
# Reads creds from ~/.telethon-alfred-e2e/creds.env (created by
# infra/setup-telethon-session.py). Never echoes them to stdout.
#
# Usage:
#   python3 infra/e2e-harness.py
#   python3 infra/e2e-harness.py --only weather-today,humanizer-strip
#   python3 infra/e2e-harness.py --json out.json
#
# The harness uses the same venv setup-telethon-session.py created.
# If telethon isn't importable, it re-execs into the venv.

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

VENV_DIR = Path.home() / ".telethon-alfred-e2e"
CREDS_FILE = VENV_DIR / "creds.env"
SESSION_FILE = VENV_DIR / "session.txt"
TESTS_FILE = Path(__file__).resolve().parent / "e2e-tests.yaml"
BOT_USERNAME = "alfred_antriksh_bot"


def maybe_reexec_in_venv():
    """If telethon isn't importable (running outside the setup venv),
    re-exec inside the setup venv so imports work."""
    venv_python = VENV_DIR / "bin" / "python3"
    if not venv_python.exists():
        sys.exit(f"ERROR: venv missing at {VENV_DIR}. Run: python3 infra/setup-telethon-session.py first")
    if sys.executable != str(venv_python):
        os.execv(str(venv_python), [str(venv_python), __file__] + sys.argv[1:])


def load_env():
    if not CREDS_FILE.exists():
        sys.exit(f"ERROR: {CREDS_FILE} missing. Run setup-telethon-session.py first.")
    creds = {}
    for line in CREDS_FILE.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    return creds


def strip_inline_comment(s: str) -> str:
    """Strip a trailing ` # ...` comment from a YAML scalar value, but
    only when not inside quotes. Naive but enough for our test file."""
    if s.startswith("'") or s.startswith('"'):
        return s
    hash_idx = s.find(" #")
    return s[:hash_idx].rstrip() if hash_idx >= 0 else s


def load_tests():
    """Tiny YAML-ish parser tuned for our test file. Avoids adding a
    yaml dep on top of telethon. Matches the same shape as
    eval/run-skill-eval.mjs's parser."""
    txt = TESTS_FILE.read_text()
    lines = txt.split("\n")
    tests = []
    cur = None
    field = None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("tests:"):
            continue
        if line.startswith("  - id:"):
            if cur is not None:
                tests.append(cur)
            cur = {"id": line.split(":", 1)[1].strip(), "expects": {}}
            field = None
        elif cur is None:
            continue
        elif line.startswith("    expects:"):
            field = "expects"
        elif field == "expects" and line.startswith("      ") and ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = strip_inline_comment(v.strip())
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                vals = []
                if inner:
                    for item in inner.split(","):
                        item = item.strip()
                        if (item.startswith("'") and item.endswith("'")) or (item.startswith('"') and item.endswith('"')):
                            item = item[1:-1]
                        vals.append(item)
                cur["expects"][k] = vals
            elif v == "":
                cur["expects"][k] = []
            else:
                # bare scalar (e.g. reply_min_len: 50)
                try:
                    cur["expects"][k] = int(v)
                except ValueError:
                    cur["expects"][k] = v
        elif line.startswith("    ") and ":" in line and field != "expects":
            k, v = line.split(":", 1)
            k = k.strip()
            v = strip_inline_comment(v.strip())
            if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                v = v[1:-1]
            try:
                cur[k] = int(v)
            except ValueError:
                cur[k] = v
            field = None
    if cur is not None:
        tests.append(cur)
    return tests


# ── assertion engine ────────────────────────────────────────────

def run_assertions(reply_text: str, expects: dict) -> list[str]:
    failures = []
    text = reply_text or ""
    text_lc = text.lower()

    if "reply_min_len" in expects and len(text) < expects["reply_min_len"]:
        failures.append(f"reply too short ({len(text)} < {expects['reply_min_len']} chars)")

    if "reply_contains" in expects:
        for needle in expects["reply_contains"]:
            if needle.lower() not in text_lc:
                failures.append(f"missing substring: {needle!r}")

    if "reply_regex" in expects:
        patterns = expects["reply_regex"]
        if isinstance(patterns, str):
            patterns = [patterns]
        for pat in patterns:
            try:
                if not re.search(pat, text, flags=re.IGNORECASE):
                    failures.append(f"regex no match: {pat}")
            except re.error as e:
                failures.append(f"regex compile error in {pat!r}: {e}")

    if "reply_not_contains" in expects:
        for needle in expects["reply_not_contains"]:
            if needle.lower() in text_lc:
                failures.append(f"unexpected substring present: {needle!r}")

    return failures


# ── status-ping filter ──────────────────────────────────────────
# OpenClaw emits in-flight progress messages from the bot's user_id
# while a turn is processing. They look like real bot replies over
# the wire and were tripping our naive "any message from the bot"
# filter, causing false-positive assertion fails.
#
# Empirically observed sentinels (extend as we see new shapes):
#   "📊 session_status"   — OpenClaw's per-turn status indicator
#   "⌛", "🤔"             — generic "thinking" emoji
#   "thinking..."         — text-form indicator
#
# Plus a safety net: replies under ~5 chars are almost always noise.

STATUS_SENTINELS = [
    "📊 session_status",
    "📊 Session status",
    "⌛",
    "🤔",
    "thinking...",
    "Thinking...",
    "processing...",
]


def is_status_ping(text: str | None) -> bool:
    t = (text or "").strip()
    if len(t) < 5:
        return True
    return any(t.startswith(s) for s in STATUS_SENTINELS)


# ── runner ──────────────────────────────────────────────────────

async def run_test(client, bot, test):
    # Default timeout bumped to 90s. OpenClaw's per-turn cost can be
    # high (744-line AGENTS.md + 49 SKILL.md files in context) and
    # gpt-5.4-mini sometimes hits rate limits and retries; both cases
    # take longer than the previous 30s default.
    timeout = test.get("timeout", 90)
    expects = test.get("expects", {})

    # Settle period BEFORE sending: ensure the bot isn't mid-turn from
    # a previous test. If we send while the bot is processing, OpenClaw
    # coalesces our message into the in-flight turn and never replies
    # to ours separately. The previous run's symptom: 11 of 13 messages
    # got coalesced and silently dropped.
    #
    # Approach: read the latest message; if it's recent (< 5s old) the
    # bot is likely still working — sleep more.
    for _ in range(20):
        latest = await client.get_messages(bot, limit=1)
        if not latest:
            break
        # `latest[0].date` is timezone-aware UTC datetime
        import datetime as dt
        age = (dt.datetime.now(dt.timezone.utc) - latest[0].date).total_seconds()
        if age >= 5:
            break
        await asyncio.sleep(3)

    sent = await client.send_message(bot, test["message"])

    # Poll for a reply from the bot. Retry every 2s up to timeout.
    # Bug #2 fix: filter out OpenClaw's status-ping messages
    # ("📊 session_status", "⌛", "🤔", etc.) so we wait for the actual
    # final reply instead of grabbing the in-flight progress indicator.
    deadline = time.time() + timeout
    started = time.time()
    reply = None
    while time.time() < deadline:
        await asyncio.sleep(2)
        msgs = await client.get_messages(bot, limit=10, min_id=sent.id)
        # Filter to bot replies that aren't status pings.
        bot_replies = [
            m for m in msgs
            if m.sender_id != sent.sender_id
            and not is_status_ping(m.text)
        ]
        if bot_replies:
            # Latest at top from get_messages; we want the earliest reply.
            reply = bot_replies[-1]
            break

    elapsed = round(time.time() - started, 1)

    if reply is None:
        return {
            "id": test["id"],
            "skill": test.get("skill", "?"),
            "pass": False,
            "elapsed_s": elapsed,
            "failures": ["timeout: no reply received"],
            "reply_text": "",
        }

    failures = run_assertions(reply.text or "", expects)
    return {
        "id": test["id"],
        "skill": test.get("skill", "?"),
        "pass": len(failures) == 0,
        "elapsed_s": elapsed,
        "failures": failures,
        "reply_text": (reply.text or "")[:300],
    }


async def main_async(args):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    creds = load_env()
    api_id = int(creds["TG_API_ID"])
    api_hash = creds["TG_API_HASH"]
    session_str = SESSION_FILE.read_text().strip()

    tests = load_tests()
    if args.only:
        wanted = set(s.strip() for s in args.only.split(","))
        tests = [t for t in tests if t["id"] in wanted]
        if not tests:
            sys.exit(f"ERROR: no tests match --only {args.only}")

    print(f"[harness] {len(tests)} test(s) queued; bot=@{BOT_USERNAME}")
    print()

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        sys.exit(
            "ERROR: Telegram session is no longer authorized (likely revoked by\n"
            "Telegram or by you via Settings → Privacy → Active sessions).\n\n"
            "Fix: re-run the setup script —\n"
            "  python3 infra/setup-telethon-session.py\n\n"
            "This will prompt for a fresh login code from Telegram and rewrite\n"
            "~/.telethon-alfred-e2e/session.txt."
        )
    try:
        try:
            bot = await client.get_entity(BOT_USERNAME)
        except Exception as e:
            sys.exit(f"ERROR: could not resolve @{BOT_USERNAME}: {e}")

        results = []
        for i, test in enumerate(tests, start=1):
            print(f"[{i}/{len(tests)}] {test['id']:30s}  →  ", end="", flush=True)
            try:
                r = await run_test(client, bot, test)
            except Exception as e:
                r = {
                    "id": test["id"],
                    "skill": test.get("skill", "?"),
                    "pass": False,
                    "elapsed_s": 0,
                    "failures": [f"harness exception: {type(e).__name__}: {e}"],
                    "reply_text": "",
                }
            results.append(r)
            tag = "✓" if r["pass"] else "✗"
            print(f"{tag} ({r['elapsed_s']}s)")
            if not r["pass"]:
                for f in r["failures"]:
                    print(f"     - {f}")
                if r["reply_text"]:
                    print(f"     reply preview: {r['reply_text'][:120]}…")
            # Long inter-test gap (60s) — preserves rate-limit headroom
            # AND avoids Telegram's "suspicious activity" auto-revocation
            # of the user session. The first harness run (15s gaps)
            # invalidated the session; this is the conservative default.
            # For mature harness runs after issue #2 is fixed, drop to 15s.
            await asyncio.sleep(60)
    finally:
        await client.disconnect()

    # Summary
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    print()
    print("═" * 60)
    print(f" {passed}/{total} passed")
    if passed < total:
        print()
        print(" Failures:")
        for r in results:
            if not r["pass"]:
                print(f"  ✗ {r['id']:30s}  {'; '.join(r['failures'])}")
    print("═" * 60)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "bot": BOT_USERNAME,
            "summary": {"passed": passed, "total": total},
            "results": results,
        }, indent=2, ensure_ascii=False))
        print(f"\n  full results → {args.json}")

    return 0 if passed == total else 1


def main():
    parser = argparse.ArgumentParser(description="E2E harness for @alfred_antriksh_bot via Telethon.")
    parser.add_argument("--only", help="comma-separated test ids to run; default = all")
    parser.add_argument("--json", help="write full result JSON to this path")
    args = parser.parse_args()

    maybe_reexec_in_venv()

    rc = asyncio.run(main_async(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
