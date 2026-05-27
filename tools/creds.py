#!/usr/bin/env python3
"""
Personal AI assistant credential vault.

Encrypted SQLite database storing credentials behind a single master key.
Every access is logged to /var/log/arete-creds/access.log with timestamp,
action, credential name, and caller.

Storage layout:
    /var/lib/arete-creds/vault.db        — encrypted credentials, mode 600
    /etc/arete-secrets/master.key        — master key (32 bytes base64), mode 600
    /var/log/arete-creds/access.log      — audit log, mode 640

Threat model (single-tenant, single-user):
    - VPS process compromise as botuser → can read vault.db AND master.key
      → can decrypt all credentials. Acceptable: matches what an attacker
      that fully compromised botuser would have anyway.
    - Filesystem snapshot / backup leak → vault.db alone is encrypted blob,
      master.key is what unlocks it. Master key is backed up separately to
      1Password (offline). So a backup-only leak (R2 snapshot, cloud-init
      copy) is just an opaque blob.
    - Anthropic/OpenAI process injection → bot reads ONE credential per
      call via subprocess. Compromised bot logic can read any credential
      it knows the name of, but every read is logged. Phase 5 adds
      per-skill scope enforcement.

Operations:
    arete-cred init               — generate master key, init empty vault
    arete-cred set NAME           — interactively prompt for value, encrypt, store
    arete-cred get NAME           — print decrypted value to stdout (no newline)
    arete-cred list               — list credential names (no values), with creation timestamps
    arete-cred delete NAME        — remove a credential
    arete-cred audit              — print the audit log
    arete-cred check              — sanity-check vault integrity (decrypt every cred)

Optional flag:
    --skill SKILLNAME           — record which skill is requesting (for audit)
                                  Phase 5 will enforce; for now logged only.

Exit codes:
    0   success
    1   credential not found / decrypt failure / unknown command
    2   vault not initialized (run `arete-cred init`)
"""

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timezone

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    sys.stderr.write(
        "ERROR: 'cryptography' package not installed. Run: pip install cryptography\n"
    )
    sys.exit(2)


VAULT_PATH = "/var/lib/arete-creds/vault.db"
MASTER_KEY_PATH = "/etc/arete-secrets/master.key"
LOG_PATH = "/var/log/arete-creds/access.log"


def _audit(action: str, name: str, skill: str | None = None, status: str = "ok") -> None:
    """Append one structured line to the audit log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    caller = os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"
    skill_field = f" skill={skill}" if skill else ""
    line = f"{ts} action={action} name={name}{skill_field} caller={caller} status={status}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        # If we can't write the log, fall back to stderr but don't fail the operation.
        sys.stderr.write(f"WARN: audit log write failed: {e}\n")


def _load_key() -> Fernet:
    """Load the master key from disk and return a Fernet instance."""
    if not os.path.exists(MASTER_KEY_PATH):
        sys.stderr.write(
            f"ERROR: master key not found at {MASTER_KEY_PATH}. Run `arete-cred init` first.\n"
        )
        sys.exit(2)
    with open(MASTER_KEY_PATH, "rb") as f:
        key = f.read().strip()
    try:
        return Fernet(key)
    except (ValueError, TypeError) as e:
        sys.stderr.write(f"ERROR: master key is invalid: {e}\n")
        sys.exit(2)


def _connect() -> sqlite3.Connection:
    """Open the vault SQLite database, creating the table if missing."""
    conn = sqlite3.connect(VAULT_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creds (
            name TEXT PRIMARY KEY,
            value BLOB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def cmd_init() -> int:
    """Generate a new master key and initialize an empty vault."""
    if os.path.exists(MASTER_KEY_PATH):
        sys.stderr.write(
            f"ERROR: master key already exists at {MASTER_KEY_PATH}. "
            "Refusing to overwrite (would invalidate all stored credentials).\n"
        )
        return 1

    # Make sure the directory exists with right perms (cloud-init pre-created it).
    key_dir = os.path.dirname(MASTER_KEY_PATH)
    os.makedirs(key_dir, mode=0o700, exist_ok=True)

    key = Fernet.generate_key()
    # Write key with mode 600 atomically.
    fd = os.open(MASTER_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)

    # Initialize the SQLite db.
    conn = _connect()
    conn.close()
    os.chmod(VAULT_PATH, 0o600)

    _audit("INIT", name="-")
    sys.stdout.write("Vault initialized.\n")
    sys.stdout.write(
        "\nMASTER KEY (back this up to 1Password as bot-vault-master-key):\n"
    )
    sys.stdout.write(key.decode() + "\n")
    sys.stdout.write(
        "\nWithout this key the vault is permanently unreadable. "
        "Master key on disk is at: " + MASTER_KEY_PATH + "\n"
    )
    return 0


def cmd_set(name: str, skill: str | None = None) -> int:
    """Prompt for a credential value (hidden), encrypt, and store."""
    value = getpass.getpass(f"Enter value for {name}: ")
    if not value:
        sys.stderr.write("ERROR: empty value, refusing to store.\n")
        _audit("SET", name, skill=skill, status="empty")
        return 1

    f = _load_key()
    enc = f.encrypt(value.encode())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = _connect()
    try:
        # Upsert: insert or update.
        existing = conn.execute(
            "SELECT created_at FROM creds WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE creds SET value = ?, updated_at = ? WHERE name = ?",
                (enc, now, name),
            )
            action = "UPDATE"
        else:
            conn.execute(
                "INSERT INTO creds (name, value, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, enc, now, now),
            )
            action = "SET"
        conn.commit()
    finally:
        conn.close()

    _audit(action, name, skill=skill)
    sys.stdout.write(f"{action.lower()}: {name}\n")
    return 0


def cmd_get(name: str, skill: str | None = None) -> int:
    """Decrypt and print one credential value."""
    f = _load_key()
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM creds WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()

    if not row:
        _audit("GET", name, skill=skill, status="missing")
        sys.stderr.write(f"ERROR: no credential named {name}\n")
        return 1

    try:
        value = f.decrypt(row[0]).decode()
    except InvalidToken:
        _audit("GET", name, skill=skill, status="decrypt_fail")
        sys.stderr.write(
            "ERROR: decrypt failed. Master key mismatch or corrupted vault.\n"
        )
        return 1

    sys.stdout.write(value)
    _audit("GET", name, skill=skill)
    return 0


def cmd_list(skill: str | None = None) -> int:
    """Print credential names + timestamps. Never prints values."""
    conn = _connect()
    try:
        rows = list(
            conn.execute(
                "SELECT name, created_at, updated_at FROM creds ORDER BY name"
            )
        )
    finally:
        conn.close()

    if not rows:
        sys.stdout.write("(empty vault)\n")
        _audit("LIST", name="-", skill=skill, status="empty")
        return 0

    sys.stdout.write(f"{'NAME':<35} {'CREATED':<22} {'UPDATED':<22}\n")
    for name, created, updated in rows:
        sys.stdout.write(f"{name:<35} {created:<22} {updated:<22}\n")
    _audit("LIST", name="-", skill=skill)
    return 0


def cmd_delete(name: str, skill: str | None = None) -> int:
    """Delete one credential."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM creds WHERE name = ?", (name,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()

    if deleted == 0:
        _audit("DELETE", name, skill=skill, status="missing")
        sys.stderr.write(f"ERROR: no credential named {name}\n")
        return 1

    _audit("DELETE", name, skill=skill)
    sys.stdout.write(f"deleted: {name}\n")
    return 0


def cmd_audit() -> int:
    """Print the audit log (newest last)."""
    if not os.path.exists(LOG_PATH):
        sys.stdout.write("(audit log empty)\n")
        return 0
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return 0


def cmd_check() -> int:
    """Sanity-check: decrypt every credential. Surfaces master-key/vault mismatches."""
    f = _load_key()
    conn = _connect()
    try:
        rows = list(conn.execute("SELECT name, value FROM creds"))
    finally:
        conn.close()

    if not rows:
        sys.stdout.write("OK (vault empty, master key valid format)\n")
        return 0

    failed = []
    for name, value in rows:
        try:
            f.decrypt(value)
        except InvalidToken:
            failed.append(name)

    if failed:
        sys.stderr.write(f"FAIL: {len(failed)} credential(s) failed to decrypt: {', '.join(failed)}\n")
        _audit("CHECK", name="-", status=f"fail:{len(failed)}")
        return 1

    sys.stdout.write(f"OK ({len(rows)} credentials, all decryptable)\n")
    _audit("CHECK", name="-")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="arete-cred",
        description="Personal AI assistant credential vault.",
    )
    parser.add_argument(
        "--skill",
        help="Skill name requesting the credential (logged for audit).",
        default=None,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Generate master key + initialize empty vault.")
    p_set = sub.add_parser("set", help="Set / update a credential (interactive).")
    p_set.add_argument("name")
    p_get = sub.add_parser("get", help="Print a credential value.")
    p_get.add_argument("name")
    sub.add_parser("list", help="List credential names + timestamps.")
    p_del = sub.add_parser("delete", help="Delete a credential.")
    p_del.add_argument("name")
    sub.add_parser("audit", help="Print the audit log.")
    sub.add_parser("check", help="Decrypt every credential to verify integrity.")

    args = parser.parse_args()
    cmd = args.cmd

    if cmd == "init":
        return cmd_init()
    if cmd == "set":
        return cmd_set(args.name, skill=args.skill)
    if cmd == "get":
        return cmd_get(args.name, skill=args.skill)
    if cmd == "list":
        return cmd_list(skill=args.skill)
    if cmd == "delete":
        return cmd_delete(args.name, skill=args.skill)
    if cmd == "audit":
        return cmd_audit()
    if cmd == "check":
        return cmd_check()
    sys.stderr.write(f"unknown command: {cmd}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
