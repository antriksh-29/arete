# Phase C end-to-end smoke test

The first moment of truth: send a message to the platform bot on Telegram, the router routes it to a manually-provisioned tenant container, the container replies through the same bot, and you get the reply.

This guide assumes:
- `arete-1` host is bootstrapped (cloud-init done, Docker running, image pulled)
- DNS for your bot domain is pointed at `arete-1`'s IP
- `arete-state-template` repo exists
- The Telegram bot has been created via BotFather

## Step 1 — Finalize host: Caddy + PLATFORM_DOMAIN

On `arete-1`:

```bash
cd ~/arete
sudo PLATFORM_DOMAIN=arete.example.com GITHUB_PAT=<from-1password> bash infra/bootstrap-host.sh
```

This installs Caddy and provisions a Let's Encrypt cert. Confirm:

```bash
curl -I https://arete.example.com/health
# expect 502 (Caddy is up but the router isn't running yet) — that's the right error
```

## Step 2 — Apply DB migrations

From your laptop OR `arete-1`:

```bash
export DATABASE_URL="postgresql://...session-mode-url-from-1password..."
bash db/migrate.sh --status   # confirm 0001_init is pending
bash db/migrate.sh            # apply
bash db/migrate.sh --status   # confirm 0001_init is applied
```

## Step 3 — Create the router env file

On `arete-1`:

```bash
sudo install -d -m 0700 /etc/arete-secrets
sudo tee /etc/arete-secrets/router.env > /dev/null <<'ENV'
DATABASE_URL=postgresql://...session-mode-url...
TELEGRAM_BOT_TOKEN=<from-1password>
TELEGRAM_WEBHOOK_SECRET=<openssl rand -hex 32>
ADMIN_API_KEY=<openssl rand -hex 32>
PORT=8080
LOG_LEVEL=info
ENV
sudo chmod 0600 /etc/arete-secrets/router.env
```

## Step 4 — Install + start the router

```bash
cd ~/arete
sudo bash infra/scripts/install-router.sh
# Expect: "✓ done." + a green systemctl status block

# Verify locally:
curl -s http://localhost:8080/health
# {"ok":true,"service":"arete-router","uptime_s":...}
```

If anything fails: `journalctl -u arete-router -f` for the full log stream.

## Step 5 — Provision a test tenant for yourself

Run this on `arete-1`. Replace `123456789` with your Telegram user id (find via @userinfobot or your own bot logs).

```bash
sudo env \
  GITHUB_PAT=<from-1password> \
  DATABASE_URL=<...session-mode...> \
  SHARED_ANTHROPIC_KEY=<from-1password-ARETE_SHARED_ANTHROPIC_KEY> \
  bash infra/scripts/provision-test-tenant.sh \
    --telegram-user-id 123456789 \
    --name "Antriksh" \
    --country "IN" \
    --timezone "Asia/Kolkata"
```

This:
- Creates `arete-<id>-assistant` repo from `arete-state-template`
- Clones it to `/var/tenants/<id>/`
- Inserts the row into `tenants` (status='active')
- `docker create`s the container (doesn't start it — router does that on demand)

## Step 6 — Point Telegram at the router

```bash
TELEGRAM_BOT_TOKEN=<from-1password> \
TELEGRAM_WEBHOOK_SECRET=<same-as-router-env> \
  bash infra/scripts/set-telegram-webhook.sh https://arete.example.com/telegram/webhook

# Verify:
TELEGRAM_BOT_TOKEN=<from-1password> bash infra/scripts/set-telegram-webhook.sh --info
# pending_update_count should be 0, url should match what you set
```

## Step 7 — The moment of truth

Open Telegram. Find your bot (the username you set with BotFather). Send `/start`.

**Expected**:
- Within ~2s: the router starts the container, waits for healthcheck, proxies the webhook
- Within ~10s total: the container replies with the SOUL.md First-Time-Conversation greeting

**If it works**: 🎉 Phase C is shipped.

**If it doesn't**: walk the chain backwards.

| Check | Command |
|---|---|
| Telegram delivered to router? | `journalctl -u arete-router -n 50` (look for the `/telegram/webhook` log line) |
| Router found tenant? | grep for `tenant: <id>` in the same logs |
| Container started? | `docker ps -a --filter name=arete-` |
| Container healthy? | `docker logs arete-<id>` |
| Did router get a healthcheck? | The router will retry for 30s; check journalctl |
| Did proxy return 200? | grep `proxied webhook to tenant` |

## Step 8 — Verify hibernation

Don't send any messages for 30 minutes. Then check:

```bash
docker ps -a --filter name=arete-
# State should be "Exited" — hibernation tick stopped it
```

Send another message in Telegram → container should auto-wake.

## Cleanup

```bash
docker rm -f arete-<id>
psql "$DATABASE_URL" -c "delete from tenants where telegram_user_id = <yours>"
sudo rm -rf /var/tenants/<id>
# Delete the GitHub repo via GitHub UI
```
