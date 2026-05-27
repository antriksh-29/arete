# services/router

The Arete tenant router. Single-purpose Node + TypeScript service that:

1. Owns the public Telegram webhook (`POST /telegram/webhook`)
2. Looks up `telegram_user_id` → `tenant` in Postgres
3. Ensures the tenant's container is running (starts it if hibernated)
4. Waits for healthcheck, then proxies the webhook to the tenant's internal port
5. Touches `last_active_at` so the hibernation tick knows the tenant is live
6. Hibernates idle containers (>30 min by default) on a once-a-minute tick

## Files

```
src/
├── index.ts        # Express bootstrap, graceful shutdown
├── config.ts       # zod-validated env (fails loud at startup)
├── log.ts          # pino logger
├── db.ts           # pg pool + tenant queries
├── docker.ts       # dockerode wrappers (start/stop/health)
├── proxy.ts        # webhook → tenant container
├── webhook.ts      # POST /telegram/webhook handler
├── lifecycle.ts    # hibernation tick
└── admin.ts        # GET /admin/tenants (auth-gated)
Dockerfile          # build the router as a container (run on host with /var/run/docker.sock mounted)
```

## Env

| Var | Required | Default | Notes |
|---|---|---|---|
| `PORT` | no | 8080 | Caddy reverse-proxies to this |
| `DATABASE_URL` | **yes** | — | Supabase session-mode URL |
| `TELEGRAM_BOT_TOKEN` | **yes** | — | From BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | no | — | If set, must match `X-Telegram-Bot-Api-Secret-Token` |
| `DOCKER_SOCKET` | no | `/var/run/docker.sock` | |
| `IMAGE_TAG` | no | `ghcr.io/antriksh-29/arete:latest` | Used by provisioner (Phase D) when creating new tenants |
| `VOLUME_ROOT` | no | `/var/tenants` | |
| `HIBERNATE_AFTER_MINUTES` | no | 30 | |
| `PORT_POOL_START` | no | 9000 | Range for tenant internal ports |
| `PORT_POOL_END` | no | 9999 | |
| `ADMIN_API_KEY` | no | — | If set, enables `/admin/*` endpoints (X-Admin-Key header) |
| `LOG_LEVEL` | no | `info` | pino levels |

## Running locally

```bash
cd services/router
npm install
export DATABASE_URL="postgresql://…"
export TELEGRAM_BOT_TOKEN="…"
npm run dev
```

## Building the image

```bash
docker build -t arete-router:dev -f services/router/Dockerfile services/router/
```

## Production deploy

Runs as a systemd service on the Arete host. See `infra/services/arete-router.service`
(landed in Phase C PR #12).

## What's NOT here

- **Provisioning** (creating tenants from scratch when an unknown user messages the bot).
  That's Phase D's `services/provisioner/`. For now, unknown users get a friendly
  "you're not on the beta" reply.
- **Per-tenant LLM token metering**: Phase F.
- **Container creation** (`docker run`): the router only `docker start`s
  already-created containers. Containers are created by the provisioner (or by
  the manual provision-test-tenant.sh helper during Phase C).
