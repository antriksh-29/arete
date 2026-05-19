"""tests/telethon/conftest.py — shared fixtures for the Telethon e2e suite.

Reads credentials from the EXISTING Telethon setup at ~/.telethon-alfred-e2e/
(produced by `python3 infra/setup-telethon-session.py`). That setup wrote:
  ~/.telethon-alfred-e2e/creds.env    — TG_API_ID, TG_API_HASH, TG_PHONE, TG_SESSION_FILE
  ~/.telethon-alfred-e2e/session.txt  — Telethon StringSession (reusable)
  ~/.telethon-alfred-e2e/bin/python3  — venv with telethon installed

We re-use that venv so the harness has zero new setup. The pytest invocation
is just `~/.telethon-alfred-e2e/bin/python3 -m pytest tests/telethon/`.

Provides:
  • `tg_client` (session-scoped): authenticated Telethon client.
  • `bot` (session-scoped): InputPeer for the bot dialog.
  • `clean_user_skill(slug)`: SSH-side teardown that removes a user-skill's
    deployed copy AND reverts the test commit on the runtime branch.
  • `preflight_check`: asserts ALFRED_SKILL_AUTHORING=on on the VPS.

Also runnable as a CLI:
  python3 tests/telethon/conftest.py --smoke       # ping the bot
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest


# ──────────────────────────────────────────────────────────────────
# Locate the existing Telethon setup
# ──────────────────────────────────────────────────────────────────

VENV_DIR = Path.home() / ".telethon-alfred-e2e"
CREDS_ENV = VENV_DIR / "creds.env"
SESSION_FILE = VENV_DIR / "session.txt"

if not CREDS_ENV.exists() or not SESSION_FILE.exists():
    pytest.exit(
        f"Telethon setup not found at {VENV_DIR}. Run:\n"
        f"    python3 infra/setup-telethon-session.py\n"
        f"to create the venv + session, then re-run pytest."
    )

# Import telethon AFTER confirming the venv exists. If we're running
# inside the venv already, this just works; if not, raise a clear error
# directing the user to use the venv's python.
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    pytest.exit(
        f"telethon not importable in this Python. Run pytest using the venv:\n"
        f"    {VENV_DIR}/bin/python3 -m pytest tests/telethon/"
    )


# ──────────────────────────────────────────────────────────────────
# Load creds from the existing creds.env
# ──────────────────────────────────────────────────────────────────

def _load_creds_env() -> dict:
    """Parse the simple KEY=VALUE lines in creds.env."""
    out = {}
    for line in CREDS_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


CREDS = _load_creds_env()

# Bot username — defaults to Alfred's prod bot. Override via env var if
# you want to point the harness at a staging bot.
BOT_USERNAME = os.environ.get("ALFRED_BOT_USERNAME", "alfred_antriksh_bot")


# ──────────────────────────────────────────────────────────────────
# Client + session
# ──────────────────────────────────────────────────────────────────

async def _make_client() -> TelegramClient:
    """Build a connected Telethon client from the existing session string."""
    session_str = SESSION_FILE.read_text().strip()
    api_id = int(CREDS["TG_API_ID"])
    api_hash = CREDS["TG_API_HASH"]
    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            f"Session at {SESSION_FILE} expired. Re-run:\n"
            f"    python3 infra/setup-telethon-session.py"
        )
    return client


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped loop so the same client survives across cases."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def tg_client():
    """Connected, authenticated Telethon client."""
    client = await _make_client()
    yield client
    await client.disconnect()


@pytest.fixture(scope="session")
async def bot(tg_client):
    """Resolved bot dialog (cached for the session)."""
    return await tg_client.get_entity(BOT_USERNAME)


# ──────────────────────────────────────────────────────────────────
# Pre-flight check
# ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def preflight_check():
    """Refuse to run the suite if ALFRED_SKILL_AUTHORING is not on."""
    if os.environ.get("SKIP_PREFLIGHT") == "1":
        return
    try:
        result = subprocess.run(
            ["ssh", "bot",
             "jq -r '.agents.defaults.env.ALFRED_SKILL_AUTHORING // \"unset\"' "
             "~/.openclaw/openclaw.json 2>/dev/null || echo unknown"],
            capture_output=True, text=True, timeout=15,
        )
        flag = result.stdout.strip()
    except subprocess.TimeoutExpired:
        pytest.skip("preflight: ssh to bot timed out — is your ~/.ssh/config set up for `ssh bot`?")
    except FileNotFoundError:
        pytest.skip("preflight: ssh not on PATH")
    if flag != "on":
        pytest.skip(
            f"preflight: ALFRED_SKILL_AUTHORING={flag!r} on VPS — "
            f"set it to 'on' before running the Telethon suite, or pass "
            f"SKIP_PREFLIGHT=1 to bypass (the suite will fail every case)."
        )


# ──────────────────────────────────────────────────────────────────
# Per-test cleanup
# ──────────────────────────────────────────────────────────────────

def _cleanup_user_skill(slug: str) -> None:
    """SSH-side: rm the deployed copy, revert any test commit, re-deploy."""
    subprocess.run(
        ["ssh", "bot",
         f"rm -rf ~/.openclaw/workspace/skills/{slug} 2>/dev/null; "
         f"rm -rf ~/personal-assistant/user-skills/{slug} 2>/dev/null; "
         f"rm -f ~/personal-assistant/brain/skills/user-skills/{slug}.md 2>/dev/null"],
        timeout=20,
    )
    # Revert any commits that touched this slug
    subprocess.run(
        ["ssh", "bot",
         f"cd ~/personal-assistant && "
         f"git log --pretty=%H -- user-skills/{slug} | head -3 | "
         f"xargs -r git revert --no-edit 2>/dev/null || true"],
        timeout=30,
    )
    # Daemon kick
    subprocess.run(
        ["ssh", "bot", "systemctl --user reload openclaw-gateway 2>/dev/null || systemctl --user restart openclaw-gateway"],
        timeout=30,
    )


@pytest.fixture
def clean_user_skill():
    """Cleanup fixture — pass a slug to schedule its teardown."""
    pending: list[str] = []
    def _register(slug: str) -> str:
        pending.append(slug)
        return slug
    yield _register
    for slug in pending:
        try:
            _cleanup_user_skill(slug)
        except Exception as e:
            print(f"[cleanup] failed for {slug}: {e}", file=sys.stderr)


# ──────────────────────────────────────────────────────────────────
# CLI: --smoke (ping the bot, expect reply)
# ──────────────────────────────────────────────────────────────────

async def _cli_smoke():
    client = await _make_client()
    try:
        bot_entity = await client.get_entity(BOT_USERNAME)
        await client.send_message(bot_entity, "/ping")
        print(f"[smoke] sent /ping to @{BOT_USERNAME}, waiting for reply (15s timeout)...")
        import time
        sent_at = time.time()
        deadline = sent_at + 15
        last_id = None
        while True:
            await asyncio.sleep(1.5)
            if asyncio.get_event_loop().time() > deadline:
                break
            async for msg in client.iter_messages(bot_entity, limit=5):
                if msg.out:
                    continue
                if msg.date.timestamp() < sent_at:
                    continue
                if last_id is not None and msg.id <= last_id:
                    continue
                print(f"[smoke] got reply: {(msg.text or '')[:200]}")
                return 0
        print("[smoke] no reply in window", file=sys.stderr)
        return 1
    finally:
        await client.disconnect()


def main_cli() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="ping the bot, expect reply")
    args = ap.parse_args()
    if args.smoke:
        return asyncio.run(_cli_smoke())
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main_cli())
