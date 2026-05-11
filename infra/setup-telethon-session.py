#!/usr/bin/env python3
# setup-telethon-session.py — one-time helper to authenticate Telethon
# against your Telegram account and produce a reusable session string.
#
# Run this on your Mac. It prompts for:
#   1. Your api_id  (from https://my.telegram.org/apps)
#   2. Your api_hash (same page)
#   3. Your phone number in international format (e.g. +919876543210)
#   4. The login code Telegram texts you (or sends via app push)
#   5. (Optional) Your 2FA password if you have one
#
# It then prints a `session_string` you save to a local .env or paste
# into the e2e harness's config. None of these values ever travel
# through git, GitHub, or chat — they live only on your Mac.
#
# Telethon will be installed automatically into a local venv if missing
# so this works zero-setup.
#
# Usage:
#   python3 infra/setup-telethon-session.py

import getpass
import os
import sys
import subprocess
from pathlib import Path

VENV_DIR = Path.home() / ".telethon-alfred-e2e"
SESSION_FILE = VENV_DIR / "session.txt"


def ensure_telethon():
    """Install Telethon into a local venv if not already there."""
    venv_python = VENV_DIR / "bin" / "python3"
    if not venv_python.exists():
        print(f"[setup] creating venv at {VENV_DIR} ...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        subprocess.run([str(venv_python), "-m", "pip", "install", "--quiet", "telethon"], check=True)
        print("[setup] telethon installed")
    # Re-exec inside the venv so the import works.
    if sys.executable != str(venv_python):
        os.execv(str(venv_python), [str(venv_python), __file__] + sys.argv[1:])


def main():
    ensure_telethon()

    # Imports only available inside the venv.
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession

    print()
    print("══════════════════════════════════════════════════════════")
    print(" Telethon session setup for Alfred E2E harness")
    print("══════════════════════════════════════════════════════════")
    print()
    print(" Get api_id and api_hash from https://my.telegram.org/apps")
    print(" (top of the page, NOT the MTProto servers / FCM section).")
    print()

    api_id = input(" api_id  (integer): ").strip()
    if not api_id.isdigit():
        sys.exit("ERROR: api_id must be an integer")

    api_hash = getpass.getpass(" api_hash (32-char hex; input hidden): ").strip()
    if len(api_hash) != 32:
        sys.exit(f"ERROR: api_hash must be 32 chars, got {len(api_hash)}")

    phone = input(" phone number (e.g. +919876543210): ").strip()
    if not phone.startswith("+"):
        sys.exit("ERROR: phone must start with + and country code")

    print()
    print(" Connecting to Telegram and requesting login code …")
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        client.start(phone=lambda: phone)
        # client.start() handled the code prompt + 2FA prompt for us.
        me = client.get_me()
        print()
        print(f" ✓ Authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or '—'})")
        print(f"   user_id: {me.id}")
        print()

        session_str = client.session.save()

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(session_str + "\n")
    SESSION_FILE.chmod(0o600)

    # Also write a .env file alongside, for the harness to source.
    env_file = VENV_DIR / "creds.env"
    env_file.write_text(
        f"TG_API_ID={api_id}\n"
        f"TG_API_HASH={api_hash}\n"
        f"TG_PHONE={phone}\n"
        f"TG_SESSION_FILE={SESSION_FILE}\n"
    )
    env_file.chmod(0o600)

    print(" ✓ Session saved.")
    print(f"   session string → {SESSION_FILE}")
    print(f"   env vars       → {env_file}")
    print()
    print(" The harness reads these files automatically. You won't be")
    print(" prompted again unless the session expires (months).")
    print()
    print(" To revoke this session later (e.g. if your laptop is lost):")
    print("   1. open Telegram on any device → Settings → Privacy → Active sessions")
    print("   2. terminate the session labeled with the api_id/title above")


if __name__ == "__main__":
    main()
