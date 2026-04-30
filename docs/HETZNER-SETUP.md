# Hetzner VPS — Detailed Setup Guide

**Verified against:** Hetzner Cloud Console, current as of 2026-04-28
**Time:** 15-20 minutes active, ~60s waiting on first boot
**Cost:** €3.79/mo (~₹350) for CX22, prorated from creation timestamp
**Sources:** [Hetzner Docs – Creating a server](https://docs.hetzner.com/cloud/servers/getting-started/creating-a-server), [Hetzner Docs – Basic Cloud Config](https://community.hetzner.com/tutorials/basic-cloud-config/), [Hetzner SSH key setup](https://community.hetzner.com/tutorials/howto-ssh-key/)

---

## Why these choices

- **CX22** (2 vCPU, 4 GB RAM, 40 GB NVMe, 20 TB traffic, €3.79/mo) — confirmed cheapest tier that comfortably runs OpenClaw + GBrain + PGLite + log shipping for one user. Don't go below this; the next tier down would force aggressive memory tuning and risk OOM during synthesis.
- **Ubuntu 24.04 LTS** — supported until 2029, current default in Hetzner image library. Matches PRD §A.1.
- **Singapore** — Hetzner added Singapore in 2024. For an IST user this is materially better latency than the EU (~100ms RTT vs ~150ms from Falkenstein). Same price, same image library, same OS support. **PRD §A.1 says Falkenstein; this is a documented update — use Singapore.**

---

## Pre-flight (do these before clicking Create Server)

### 1. Generate SSH key on your local machine (if you don't have one)

```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null && echo "have one" || ssh-keygen -t ed25519 -C "antriksh-bot"
```

Press Enter for the default path, leave passphrase empty (acceptable for personal use; alternative is a passphrase + `ssh-agent`). The public key file is `~/.ssh/id_ed25519.pub`.

```bash
cat ~/.ssh/id_ed25519.pub
```

Copy the entire single line (starts with `ssh-ed25519`, ends with the comment). You'll paste this twice: once into Hetzner SSH keys, once into our `infra/cloud-init.yaml`.

### 2. Update `infra/cloud-init.yaml` with your SSH key

The cloud-init in this repo has a placeholder. Replace it with your actual key value before pasting into Hetzner:

```bash
# Open the file
$EDITOR /Users/aj/conductor/workspaces/personal-assistant/bogota/infra/cloud-init.yaml
```

Find this line:
```yaml
- ssh-ed25519 REPLACE_WITH_YOUR_PUBLIC_KEY antriksh-bot
```

Replace it with your real public key. Keep the `- ` (YAML list dash) prefix and the indentation. Save.

> **Why two places?** Hetzner uses the SSH key it has registered for the *initial* root login during provisioning. Cloud-init then creates `botuser` with the SSH key embedded in user-data. After cloud-init runs, `root` login is disabled and only `botuser` works — by design.

### 3. Have a payment method ready

Hetzner requires a credit/debit card or PayPal verified during account creation. Prepaid debit cards often get rejected. First-time accounts typically have a €20-50 spending cap until verified — fine for our use.

---

## Step-by-step Console flow

### 1. Sign up / sign in

Open https://console.hetzner.cloud. Sign up if new, or sign in. New accounts may need to verify via email + phone.

### 2. Create a project

If you have no projects: click **+ New Project**, name it `antriksh-bot`. If you already use Hetzner for other things, the project boundary keeps billing/access clean.

### 3. Add your SSH key (project-level)

Left sidebar → **Security** → **SSH Keys** tab → **Add SSH Key**.

- **SSH Key:** paste the entire `id_ed25519.pub` content (starts with `ssh-ed25519 AAAA...`)
- **Name:** `antriksh-bot-laptop` (or whatever names your machine)
- Click **Add SSH Key**

You should now see one row in the SSH Keys list. The fingerprint shown (e.g., `SHA256:Xy...`) should match `ssh-keygen -lf ~/.ssh/id_ed25519.pub` on your laptop. If not, you pasted the wrong key.

### 4. Create the server

Left sidebar → **Servers** → **Add Server**. The creation page has these sections from top to bottom:

#### 4a. Location
Click the **Singapore** card. It's the lowest-latency option for an Indian user.

(Falkenstein/Nuremberg/Helsinki/Hillsboro/Ashburn also work but have ~50-150ms more RTT.)

#### 4b. Image
- Tab: **OS Images** (default tab)
- Select: **Ubuntu** → **24.04**

#### 4c. Type (server plan)
- Section toggle at top: **Shared vCPU** (not Dedicated vCPU — way more expensive)
- Architecture toggle: **x86 (Intel/AMD)**
- Card: **CX22** — `€3.79 monthly · 2 VCPU · 4 GB RAM · 40 GB Disk · 20 TB Traffic`

If the Singapore region prices look slightly different (Hetzner sometimes has minor regional deltas), it's still a few cents.

#### 4d. Networking
Default — **Public IPv4 + IPv6**, both checked. The cost is included in CX22; do not disable IPv4 (Telegram and several APIs are IPv4-only in some egress paths).

#### 4e. SSH Keys
Check the box next to your `antriksh-bot-laptop` key. (You can also add a fresh key here, but we already added it project-level in step 3.)

#### 4f. Volumes / Firewalls / Backups / Placement Groups / Labels
- **Volumes:** none (the 40 GB included disk is plenty)
- **Firewalls:** none yet (we set UFW inside the OS via cloud-init; Hetzner Cloud Firewalls are an additional layer you can add post-Phase-1 if you want belt-and-braces)
- **Backups:** **DO NOT enable** Hetzner backups (€0.76/mo extra). PRD already covers backups via R2 and git push — Hetzner snapshots would be redundant cost
- **Placement Groups:** none (single-server deployment)
- **Labels:** optional. If you want to be tidy: key=`role`, value=`bot`

#### 4g. Cloud config (the important part)
Scroll to the very bottom. There's a text field labeled **Cloud config** with help text "Cloud-init helps with early initialization."

Open `infra/cloud-init.yaml` from this repo. **Make sure you've replaced the SSH key placeholder line per step 2 of pre-flight.** Copy the entire file contents and paste into the Cloud config field.

Hetzner enforces a 32 KiB limit on this field. Our file is ~2.5 KiB; you're well under.

#### 4h. Name
Field: **Name** — set to `bot-1`. Must follow RFC 1123 (alphanumeric + hyphens + periods, no spaces, no underscores, min 2 chars). `bot-1` is fine.

#### 4i. Final review
Right side panel shows: location, image, type, networking, SSH key, name, total monthly cost. Sanity check all of these.

#### 4j. Buy
Click **CREATE & BUY NOW**. The button literally says that — Hetzner is direct about charging your card.

### 5. Wait ~60-90 seconds

You'll be redirected to the server's overview page. The **Status** column shows "Initializing" → "Starting" → "Running". Cloud-init runs on first boot; for our config it takes ~30-60 seconds extra after status turns green (apt update + package install).

Note the **IPv4** address (right side panel). Looks like `5.223.45.67` or similar.

---

## Post-creation verification

### 1. Update DNS

Cloudflare DNS panel → your zone → edit the `bot` A record from §0.2 of the checklist. Change target IP from your placeholder to the actual VPS IPv4. Orange cloud OFF (proxy disabled). Save.

Verify from your laptop:
```bash
dig +short bot.antriksh.xyz
# Should print the VPS IP within 1-2 min
```

### 2. SSH in

```bash
ssh botuser@<vps-ip>
```

First time you'll get a "host authenticity" prompt — type `yes`. You should land at:
```
botuser@bot-1:~$
```

If it asks for a password, cloud-init hasn't completed yet. Wait 90 seconds, retry.

If it says "Permission denied (publickey)", either the SSH key wasn't pasted correctly into cloud-init, or you're using a different local key than the one in the Hetzner SSH keys panel. Test:
```bash
ssh -i ~/.ssh/id_ed25519 -v botuser@<vps-ip> 2>&1 | grep "Offering\|Authentications\|denied"
```

### 3. Sanity-check cloud-init

Once in via SSH:
```bash
# Marker file from our cloud-init runcmd
cat /var/log/cloud-init-bot-marker.log
# → "2026-04-28T12:34:56Z cloud-init complete"

# Verify firewall
sudo ufw status
# → Status: active, 22/tcp ALLOW, 80/tcp ALLOW, 443/tcp ALLOW

# Verify pre-created dirs
ls -la /var/lib/bot-creds /var/log/openclaw /var/log/bot-creds
# → all owned by botuser:botuser

# Verify sudo passwordless works
sudo -n true && echo "sudo OK"

# Verify SSH hardening applied
sudo grep -E "^(PermitRootLogin|PasswordAuthentication|AllowUsers)" /etc/ssh/sshd_config.d/99-bot-hardening.conf
# → PermitRootLogin no
# → PasswordAuthentication no
# → AllowUsers botuser

# Verify timezone
date
# → IST (e.g., "Tue Apr 28 18:42:11 IST 2026")
```

If any of these are wrong, see "Common issues" below.

### 4. (Optional) Set up SSH config alias on your laptop

So you can `ssh bot` instead of typing the IP each time:

```bash
cat >> ~/.ssh/config <<EOF

Host bot
    HostName <vps-ip>
    User botuser
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
EOF
```

Now `ssh bot` works.

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| SSH asks for password | cloud-init still running, OR public key mismatch | Wait 90s; if persistent, check `/var/log/cloud-init-output.log` as root |
| `Permission denied (publickey)` | Wrong SSH key in cloud-init `users.ssh_authorized_keys` | Destroy server, fix key in `infra/cloud-init.yaml`, recreate. Cheaper than debugging in place. |
| `sudo: a password is required` | cloud-init `users[].sudo` not parsed | Check YAML indent; should be `sudo: ALL=(ALL) NOPASSWD:ALL` exactly |
| `dig` returns old IP | DNS cache | Try `dig @1.1.1.1 bot.antriksh.xyz +short` to bypass local cache |
| Server stuck at "Initializing" >5 min | Hetzner platform hiccup, very rare | Wait 10 min; if still stuck, contact Hetzner support — they're responsive |
| Hetzner card charged but server failed | Provisioning failure mid-flow | Server bills are minute-pro-rated; just delete and recreate, you'll lose <€0.01 |
| Can't paste cloud-init (>32 KiB) | Unlikely with our config (~2.5 KiB) | If you've added a lot, host the YAML somewhere and reference via `#include https://...` per Hetzner docs |

---

## Cost transparency

| What | Cost | When charged |
|---|---|---|
| CX22 (Singapore) | €3.79/mo | Per-minute pro-rated; charged at end of billing month |
| IPv4 included in CX22 | €0 | (€0.50/mo if attached as standalone IP, but bundled here) |
| 20 TB bandwidth included | €0 | We'll use <50 GB/mo for one user |
| Backups (off) | €0 | We use R2 + git instead |
| If you delete and recreate same day | <€0.01 charged for the killed server | Per-minute billing |

So worst case if you mess up: you've spent a couple of cents and 5 minutes.

---

## Sources

- [Hetzner Docs — Creating a server](https://docs.hetzner.com/cloud/servers/getting-started/creating-a-server)
- [Hetzner Community — Basic Cloud Config](https://community.hetzner.com/tutorials/basic-cloud-config/)
- [Hetzner Community — Setting up an SSH key](https://community.hetzner.com/tutorials/howto-ssh-key/)
- [Hetzner Cloud pricing page](https://www.hetzner.com/cloud) — verified CX22 €3.79/mo
- [Hetzner Cloud — Singapore expansion announcement (2024)](https://www.hetzner.com/news/expansion-singapore/)
