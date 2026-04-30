# Phase 0 — Pre-build Checklist

**Goal:** complete all real-world dependencies before Session 2.
**Active work:** ~2-4 hours · **Elapsed time:** up to 24h waiting on DNS / API approvals.
**Cost:** ~₹450 one-time + ~₹350/mo from VPS provision.

Tick as you go. When all "✅ Done definition" boxes at the bottom are ticked, return for Session 2.

---

## 0.1 GitHub repos (5 min)

- [x] `antriksh-brain` — created 2026-04-28 — https://github.com/antriksh-29/antriksh-brain
- [x] `antriksh-bot-config` — created 2026-04-28 — https://github.com/antriksh-29/antriksh-bot-config

Both private. Created via `gh` CLI in Session 1.

### PAT (manual — must be done in browser)

Fine-grained PATs cannot be generated via API. Do this in your browser:

1. Open https://github.com/settings/personal-access-tokens/new
2. Token name: `antriksh-bot-backup`
3. Expiration: 1 year
4. Repository access → **Only select repositories** → pick both `antriksh-brain` and `antriksh-bot-config`
5. Permissions → Repository → set:
   - **Contents:** Read and write
   - **Metadata:** Read-only (auto-enabled)
6. Generate token. Copy the value (starts with `github_pat_`).
7. Save it in 1Password/Bitwarden as `GITHUB_BACKUP_TOKEN`. We'll seed it into the vault in Phase 2.

---

## 0.2 Domain (10 min, DNS may take up to 1h)

Buy a `.xyz` domain via Cloudflare Registrar (~₹100/yr). Or use a subdomain on a domain you already own.

- [ ] Domain registered (or subdomain decided): the bot webhook URL will be `bot.<yourdomain>.xyz`
- [ ] Cloudflare DNS panel → add an **A record**: name `bot`, target a placeholder IP for now (we'll update after Hetzner provisions). TTL: 1 min for fast iteration.
- [ ] **Important:** orange cloud OFF for the `bot` record. We need raw DNS, not Cloudflare proxy. The proxy interferes with Telegram's webhook signature path.

---

## 0.3 API keys (30-60 min, some have approval delays)

Sign up + obtain. Store values in 1Password/Bitwarden temporarily — they go into the vault during Phase 2.

| Status | Key name | Service | Cost notes | Where |
|---|---|---|---|---|
| ☐ | `ANTHROPIC_API_KEY` | Anthropic Console | ~₹250-600/mo usage | console.anthropic.com |
| ☐ | `OPENAI_API_KEY` | OpenAI Platform | ~₹150-450/mo usage | platform.openai.com |
| ☐ | `GROQ_API_KEY` | Groq Cloud | Free tier covers single-user Whisper | console.groq.com |
| ☐ | `DEEPSEEK_API_KEY` | DeepSeek Platform | Cheap fallback | platform.deepseek.com |
| ☐ | `NOTION_API_TOKEN` | Notion Integrations | Free | notion.so/my-integrations |
| ☐ | `LINEAR_API_KEY` | Linear → Settings → API | Free | linear.app/settings/api |
| ☐ | `CLOUDFLARE_API_TOKEN` | CF dashboard → My Profile → API Tokens | Free; scope: Zone:DNS:Edit on your zone | dash.cloudflare.com |
| ☐ | `CLOUDFLARE_R2_ACCESS_KEY` + `_SECRET_KEY` | R2 → Manage R2 API Tokens | ~₹5/mo backups | dash.cloudflare.com/r2 |
| ☐ | `AXIOM_API_TOKEN` | Axiom → Settings → API Tokens | Free 500GB/mo | app.axiom.co |
| ☐ | `WEATHER_API_KEY` | OpenWeatherMap | Free tier (60 calls/min) | openweathermap.org/api |

**Notion:** create an internal integration. After getting the token, **share** the workspace pages you want the bot to access via the integration's "Connections" panel.

**Cloudflare R2:** create a bucket named `bot-backups`. When generating R2 API tokens, scope to that bucket only.

**Axiom:** create a dataset named `bot-logs`. Note the ingest URL (e.g., `https://api.axiom.co/v1/datasets/bot-logs/ingest`) — we'll use it in Phase 2.

---

## 0.4 Telegram (10 min)

- [ ] Open Telegram, message **@BotFather**, send `/newbot`
- [ ] Pick a name (e.g., "Antriksh Assistant") and a unique username ending in `bot` (e.g., `antriksh_assist_bot`)
- [ ] Save the bot token returned. This is `TELEGRAM_BOT_TOKEN`
- [ ] In BotFather: `/setprivacy` → select your bot → **Enable** (bot only sees messages addressed to it; required for non-allowlist defense in depth)
- [ ] Optional: `/setdescription`, `/setabouttext`, `/setuserpic` for polish
- [ ] Get your numeric Telegram user ID: search **@userinfobot**, send `/start`, copy the numeric ID. This is `TELEGRAM_USER_ID`

> Do NOT add the bot to any group chat. PRD says bot ignores groups; easier to never test it.

---

## 0.5 Google OAuth (15 min)

For Gmail and Calendar access in Phase 6.

- [ ] console.cloud.google.com → create new project "antriksh-bot"
- [ ] APIs & Services → Library → enable: **Gmail API** and **Google Calendar API**
- [ ] OAuth consent screen → External, **test user = your email only**, do NOT publish
- [ ] Credentials → Create Credentials → OAuth Client ID → **Desktop app** (not web; we'll do device-flow OAuth from the VPS)
- [ ] Download the credentials JSON. Extract `client_id` and `client_secret`
- [ ] Save as `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`. Same values reused for `GCAL_OAUTH_CLIENT_ID`, `GCAL_OAUTH_CLIENT_SECRET` (one OAuth client, multiple scopes)

> Refresh tokens are obtained during Phase 2 via a one-time bootstrap script. You don't need them yet.

---

## 0.6 Hetzner VPS (15-20 min)

**👉 Detailed step-by-step guide:** [`docs/HETZNER-SETUP.md`](./HETZNER-SETUP.md)

That guide is verified against the current Hetzner Console UI (April 2026) and updates the PRD's Falkenstein default to **Singapore** (lower latency for IST users; Hetzner added Singapore in 2024).

Quick summary of what you'll do there:

- [ ] Generate an `ed25519` SSH key on your laptop (or confirm you have one)
- [ ] **Replace the placeholder SSH key in `infra/cloud-init.yaml` with your real public key** (skip this and you can't SSH in)
- [ ] Sign in to console.hetzner.cloud → create project `antriksh-bot`
- [ ] Add SSH key under Security → SSH Keys
- [ ] Servers → Add Server:
  - Location: **Singapore** (override of PRD's Falkenstein for latency)
  - Image: Ubuntu 24.04
  - Type: Shared vCPU → x86 → **CX22**
  - Backups: **OFF** (we use R2 + git, paying €0.76/mo for Hetzner backups would be redundant)
  - SSH Keys: tick yours
  - Cloud config: paste contents of `infra/cloud-init.yaml`
  - Name: `bot-1`
- [ ] Click **CREATE & BUY NOW**. Wait ~60-90s
- [ ] Note the IPv4 address; update the Cloudflare A record from §0.2 to point at it
- [ ] Verify: `ssh botuser@<vps-ip>` lands at a shell prompt
- [ ] Verify: `dig +short bot.<yourdomain>.xyz` returns the VPS IP (1-5 min)

For verification commands, common issues, cost transparency, and screenshots-equivalent UI labels, see the detailed guide.

---

## 0.7 Optional but useful

- [ ] Install **1Password** or **Bitwarden** if you don't already have one — vault master key backs up here in Phase 2
- [ ] Sign up for **UptimeRobot** (free) — used in Phase 6 for `/health` monitoring
- [ ] Bookmark Axiom + Hetzner consoles in a "bot ops" folder

---

## ✅ Done definition for Session 2 readiness

You're ready when **all** of these are true:

- [ ] `ssh botuser@<vps-ip>` lands at a shell (cloud-init succeeded)
- [ ] `dig +short bot.<yourdomain>.xyz` returns the VPS IP
- [ ] All 14 keys/tokens from §0.3 + §0.4 + §0.5 stored in 1Password/Bitwarden
- [ ] Both GitHub repos exist (empty is fine)
- [ ] `GITHUB_BACKUP_TOKEN` PAT generated

When all 5 boxes are ticked, open a new Conductor session and say **"Phase 0 done, Session 2 Phase 1 go."** I'll have the rest of the Phase 1 setup ready.

---

## Total real-world cost incurred so far

| Item | One-time | Recurring |
|---|---|---|
| Domain (1 year) | ~₹100 | ~₹100/yr |
| Hetzner CX22 (prorated first month) | ~₹200 | ~₹350/mo |
| API key signups | ₹0 | usage-based, target ~₹500-1500/mo total |

---

## Common gotchas

- **Hetzner cloud-init didn't run:** SSH as `root@<ip>`, check `/var/log/cloud-init-output.log`. Most common cause is YAML indent error from copy-paste. Fix the YAML and recreate the VM (cheaper than debugging in place for this stage).
- **DNS not propagating:** Cloudflare propagates in <1 min. If `dig` shows old/no record, double-check it was saved + orange cloud is OFF for `bot` subdomain.
- **OpenAI signup blocked:** sometimes requires phone verification. Anthropic should be instant.
- **DeepSeek signup blocked from India:** may need a VPN for signup. If genuinely blocked, skip — it's an emergency fallback only, the system works without it.
- **Telegram bot not responding to /start:** expected — there's no bot server yet. We're just collecting the token.
