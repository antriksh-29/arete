# Phase 2A — Vault deploy guide

Step-by-step. Total time ~25 min. Most of it is interactive credential paste.

## Prerequisites

- Phase 1 complete (you have `ssh bot` working)
- All 12 credentials available in 1Password (Phase 0 done)

## Step 2A.1 — Push the vault code from laptop to VPS

**On your laptop:**

```bash
ssh bot 'mkdir -p ~/tools'
scp /Users/aj/conductor/workspaces/personal-assistant/bogota/tools/creds.py bot:~/tools/
scp /Users/aj/conductor/workspaces/personal-assistant/bogota/tools/bot-cred bot:/tmp/bot-cred
```

Expected: 2 lines ending in `100% ...`.

## Step 2A.2 — Install the wrapper + Python dependency on VPS

**SSH in:** `ssh bot`

Then on the VPS:

```bash
# Install the cryptography library (used for AES-128 + HMAC encryption).
# pip install --user keeps it in botuser's home, not system-wide.
pip install --user --break-system-packages cryptography

# Install the bot-cred wrapper system-wide so it's on PATH.
sudo mv /tmp/bot-cred /usr/local/bin/bot-cred
sudo chown root:root /usr/local/bin/bot-cred
sudo chmod 755 /usr/local/bin/bot-cred

# Make creds.py executable (defense in depth; the wrapper invokes it via python3 anyway).
chmod 700 ~/tools/creds.py

# Verify
which bot-cred
bot-cred --help | head -5
```

**Expected:**
- `which bot-cred` prints `/usr/local/bin/bot-cred`
- `bot-cred --help` prints the usage banner

If `pip install` fails with "externally-managed-environment", the `--break-system-packages` flag is what bypasses Ubuntu 24.04's PEP 668 protection. That flag is safe here because we're installing into `botuser`'s home (`--user`), not the system Python.

## Step 2A.3 — Initialize the vault

```bash
bot-cred init
```

**Expected output:**

```
Vault initialized.

MASTER KEY (back this up to 1Password as bot-vault-master-key):
gAAAAAB...46-character-base64-string...=

Without this key the vault is permanently unreadable. Master key on disk is at: /etc/bot-secrets/master.key
```

### CRITICAL — back up the master key NOW

The 44-character string after "MASTER KEY:" is the only thing that can decrypt your vault. If the VPS dies and you don't have this key elsewhere, every encrypted credential is permanently lost.

**Copy that exact string into 1Password as a new Secure Note:**
- Title: `bot-vault-master-key`
- Content: the 44-character key
- Tags: `bot`, `vault`, `do-not-lose`

Verify by re-reading from 1Password before continuing. If you skip this step and the VPS dies tomorrow, you redo Phase 0+2 from scratch.

## Step 2A.4 — Confirm permissions and ownership

```bash
ls -la /etc/bot-secrets/master.key
ls -la /var/lib/bot-creds/vault.db
ls -la /var/log/bot-creds/access.log
```

**Expected:**
```
-rw------- 1 botuser botuser 44   ... /etc/bot-secrets/master.key
-rw------- 1 botuser botuser 8192 ... /var/lib/bot-creds/vault.db
-rw-r----- 1 botuser botuser 80+  ... /var/log/bot-creds/access.log
```

The master key and vault must be `600` (owner-read-write only). If anything is wider, fix:

```bash
chmod 600 /etc/bot-secrets/master.key /var/lib/bot-creds/vault.db
```

## Step 2A.5 — Seed the 12 credentials

Run each command. Each prompts for the value (input is hidden — your typing won't show).

Open 1Password in a separate window. For each line below, paste the corresponding value when `bot-cred set` prompts.

```bash
bot-cred set ANTHROPIC_API_KEY
bot-cred set OPENAI_API_KEY
bot-cred set GROQ_API_KEY
bot-cred set WEATHER_API_KEY

bot-cred set TELEGRAM_BOT_TOKEN
bot-cred set TELEGRAM_USER_ID

bot-cred set CLOUDFLARE_R2_ACCESS_KEY
bot-cred set CLOUDFLARE_R2_SECRET_KEY
bot-cred set CLOUDFLARE_R2_ENDPOINT

bot-cred set AXIOM_API_TOKEN
bot-cred set AXIOM_ORG_ID

bot-cred set GITHUB_BACKUP_TOKEN
```

After each, you'll see `set: NAME` confirming it was stored.

**Tip:** if you mistype a value, just rerun `bot-cred set NAME` — it does an upsert (replaces the existing entry with the new value). The audit log will show two entries (UPDATE the second time), which is correct.

## Step 2A.6 — Verify everything works

```bash
# List names — values are not printed, just names + timestamps
bot-cred list
```

**Expected:** 12 rows, each with a name + created/updated timestamps.

```bash
# Decrypt-test every credential at once
bot-cred check
```

**Expected:** `OK (12 credentials, all decryptable)`. If this fails, paste the error to me.

```bash
# Test fetching a single value (sends raw value to stdout)
bot-cred get TELEGRAM_USER_ID
echo  # newline so the next prompt isn't on the same line
```

**Expected:** your Telegram numeric user ID, exactly as you stored it.

```bash
# Confirm the audit log has entries
bot-cred audit | tail -20
```

**Expected:** lines like:
```
2026-04-29T... action=INIT name=- caller=botuser status=ok
2026-04-29T... action=SET name=ANTHROPIC_API_KEY caller=botuser status=ok
2026-04-29T... action=SET name=OPENAI_API_KEY caller=botuser status=ok
...
2026-04-29T... action=GET name=TELEGRAM_USER_ID caller=botuser status=ok
2026-04-29T... action=CHECK name=- caller=botuser status=ok
```

## ✅ Block 2A done definition

- [ ] `bot-cred --help` works on the VPS
- [ ] Master key backed up to 1Password as `bot-vault-master-key`
- [ ] `/etc/bot-secrets/master.key` is `0600 botuser:botuser`
- [ ] `/var/lib/bot-creds/vault.db` is `0600 botuser:botuser`
- [ ] `bot-cred list` shows all 12 credentials
- [ ] `bot-cred check` returns `OK (12 credentials, all decryptable)`
- [ ] `bot-cred get TELEGRAM_USER_ID` prints your numeric user ID
- [ ] `bot-cred audit` shows the SET/GET activity

When all 8 boxes are ticked, paste:
- The output of `bot-cred list`
- The output of `bot-cred check`

That's enough proof everything's wired up. We move to Block 2B (AGENTS.md).

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `cryptography: package not installed` | `pip install` step skipped or failed | Re-run the `pip install --user --break-system-packages cryptography` line |
| `master key not found at /etc/bot-secrets/master.key` | Skipped `bot-cred init` | Run `bot-cred init`, save the master key to 1Password |
| `Permission denied` on master.key write | `/etc/bot-secrets` not owned by botuser | `sudo chown botuser:botuser /etc/bot-secrets` then retry init |
| `decrypt failed` on `bot-cred check` | Master key was rotated without re-encrypting | Don't ever overwrite master.key once vault has data; restore the original from 1Password |
| `bot-cred: command not found` | Wrapper not on PATH | Check `ls -l /usr/local/bin/bot-cred` and confirm it's executable |
