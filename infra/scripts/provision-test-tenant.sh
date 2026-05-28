#!/usr/bin/env bash
# provision-test-tenant.sh — manually create a tenant before the real provisioner ships (Phase D).
#
# What it does:
#   1. Creates a GitHub state repo for the tenant from arete-state-template
#   2. Clones the repo into /var/tenants/<short_id>
#   3. Allocates an internal_port from 9000-9999
#   4. INSERTs the tenant row into Postgres (status='active')
#   5. Creates the container with a stable name and the right env
#
# Does NOT start the container — the router will start it on first message.
#
# Usage:
#   sudo bash infra/scripts/provision-test-tenant.sh \
#     --telegram-user-id 123456789 \
#     --name "Alice Test" \
#     --country "US"
#
# Required env (set in your shell or /etc/arete-secrets):
#   GITHUB_PAT                 — for creating the GitHub repo
#   DATABASE_URL               — Postgres for the tenants table
#   SHARED_ANTHROPIC_KEY       — passed into the tenant container (creds vault later)
#   GHCR_USER (default antriksh-29)
#   IMAGE_TAG (default ghcr.io/antriksh-29/arete:latest)

set -euo pipefail

TELEGRAM_USER_ID=""
NAME=""
COUNTRY="US"
USERNAME=""
TIMEZONE="Etc/UTC"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --telegram-user-id) TELEGRAM_USER_ID="$2"; shift 2 ;;
    --name)             NAME="$2"; shift 2 ;;
    --country)          COUNTRY="$2"; shift 2 ;;
    --username)         USERNAME="$2"; shift 2 ;;
    --timezone)         TIMEZONE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'
log()  { echo "${green}[provision]${rst} $*"; }
warn() { echo "${yellow}[provision]${rst} $*"; }
die()  { echo "${red}[provision]${rst} $*" >&2; exit 1; }

[ -n "$TELEGRAM_USER_ID" ] || die "--telegram-user-id is required"
[ -n "$NAME" ]             || die "--name is required"

# Source persistent provisioner secrets if env vars aren't already set.
# /etc/arete-secrets/provisioner.env is the durable place for keys that
# would otherwise have to be passed on the command line every time.
# CLI env overrides the file (deliberate: easy to test with a different key).
if [ -f /etc/arete-secrets/provisioner.env ]; then
  set -a
  # shellcheck disable=SC1091
  : "${GITHUB_PAT:=$(grep -E '^GITHUB_PAT=' /etc/arete-secrets/provisioner.env | cut -d= -f2-)}"
  : "${SHARED_ANTHROPIC_KEY:=$(grep -E '^SHARED_ANTHROPIC_KEY=' /etc/arete-secrets/provisioner.env | cut -d= -f2-)}"
  : "${SHARED_DEEPSEEK_KEY:=$(grep -E '^SHARED_DEEPSEEK_KEY=' /etc/arete-secrets/provisioner.env | cut -d= -f2-)}"
  : "${SHARED_MOONSHOT_KEY:=$(grep -E '^SHARED_MOONSHOT_KEY=' /etc/arete-secrets/provisioner.env | cut -d= -f2-)}"
  set +a
fi

: "${GITHUB_PAT:?GITHUB_PAT required (read:packages + repo scope) — set in env OR /etc/arete-secrets/provisioner.env}"
: "${DATABASE_URL:?DATABASE_URL required}"
[ "${GITHUB_PAT}" = "PLACEHOLDER_set_me" ] && die "GITHUB_PAT in provisioner.env is still the placeholder — set the real value"
GHCR_USER="${GHCR_USER:-antriksh-29}"
IMAGE_TAG="${IMAGE_TAG:-ghcr.io/antriksh-29/arete:latest}"

# Tenant id: short uuid-ish slug from the telegram user id (stable across reprovisions for the same user)
TENANT_ID="$(printf '%s' "$TELEGRAM_USER_ID" | sha1sum | head -c 12)"
CONTAINER_NAME="arete-${TENANT_ID}"
VOLUME_PATH="/var/tenants/${TENANT_ID}"
REPO_NAME="arete-${TENANT_ID}-assistant"
REPO_URL="https://github.com/${GHCR_USER}/${REPO_NAME}.git"

log "tenant_id=${TENANT_ID}"
log "container=${CONTAINER_NAME}"
log "volume=${VOLUME_PATH}"
log "repo=${REPO_URL}"

# ── 1. Allocate an internal_port ────────────────────────────────────────────
log "allocating internal_port"
INTERNAL_PORT=$(psql "$DATABASE_URL" -At <<SQL
with used as (select coalesce(internal_port, 0) as p from tenants)
select min(p) from generate_series(9000, 9999) g(p)
where not exists (select 1 from used u where u.p = g.p);
SQL
)
[ -n "$INTERNAL_PORT" ] && [ "$INTERNAL_PORT" != "" ] || die "no free port in 9000-9999"
log "internal_port=${INTERNAL_PORT}"

# ── 2. Create the GitHub repo from template ──────────────────────────────────
log "creating github repo ${REPO_NAME} from template"
HTTP_CODE=$(curl -sS -o /tmp/repo.json -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer ${GITHUB_PAT}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${GHCR_USER}/arete-state-template/generate" \
  -d "{\"owner\":\"${GHCR_USER}\",\"name\":\"${REPO_NAME}\",\"private\":true,\"description\":\"Arete tenant state for ${NAME}\"}")

if [ "$HTTP_CODE" = "201" ]; then
  log "repo created"
elif [ "$HTTP_CODE" = "422" ] && grep -qi 'name already exists' /tmp/repo.json; then
  warn "repo already exists — reusing"
else
  die "github repo create failed (HTTP ${HTTP_CODE}): $(cat /tmp/repo.json)"
fi

# Wait a beat for GitHub to actually materialize the repo
for i in 1 2 3 4 5; do
  curl -sf -H "Authorization: Bearer ${GITHUB_PAT}" \
    "https://api.github.com/repos/${GHCR_USER}/${REPO_NAME}" >/dev/null && break
  sleep 1
done

# ── 3. Clone into volume ─────────────────────────────────────────────────────
# CRITICAL: The container's agent user is UID 1001 (set in Dockerfile). The volume
# must be owned by 1001:1001 — not host arete (UID 1000) — or every git op inside
# the container fails with "fatal: detected dubious ownership".
log "cloning into ${VOLUME_PATH}"
install -d -m 0755 "$VOLUME_PATH"
AUTH_URL="https://x-access-token:${GITHUB_PAT}@github.com/${GHCR_USER}/${REPO_NAME}.git"
if [ ! -d "${VOLUME_PATH}/.git" ]; then
  git clone "$AUTH_URL" "$VOLUME_PATH"
else
  log "volume already has .git — skipping clone"
fi

# ── 4. Render templates with tenant data ─────────────────────────────────────
log "rendering meta.json + USER.md"
TENANT_UUID="$(uuidgen | tr 'A-Z' 'a-z')"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat > "${VOLUME_PATH}/meta.json" <<META
{
  "version": 1,
  "tenant_id": "${TENANT_UUID}",
  "created_at": "${NOW_ISO}",
  "image_version": "${IMAGE_TAG}",
  "platform": "arete"
}
META

# Lightly fill USER.md with the known fields. The First-Time-Conversation flow
# inside the container will collect the rest.
if [ -f "${VOLUME_PATH}/identity/USER.md" ]; then
  perl -i -pe "s|_to be set during onboarding_|${NAME}|" "${VOLUME_PATH}/identity/USER.md"
fi

# Commit + push the rendered files so they're TRACKED in the remote. Otherwise the
# entrypoint's `git pull` sees an untracked meta.json colliding with the template's
# tracked meta.json and aborts the merge.
log "committing rendered files"
git -c safe.directory="${VOLUME_PATH}" -C "${VOLUME_PATH}" add meta.json identity/USER.md 2>/dev/null || true
if ! git -c safe.directory="${VOLUME_PATH}" -C "${VOLUME_PATH}" diff --cached --quiet; then
  git -c safe.directory="${VOLUME_PATH}" -C "${VOLUME_PATH}" \
      -c user.email="provisioner@arete.local" \
      -c user.name="Arete Provisioner" \
      commit -m "provision: render tenant ${TENANT_UUID} meta + identity" >/dev/null
  git -c safe.directory="${VOLUME_PATH}" -C "${VOLUME_PATH}" push origin HEAD:main 2>&1 \
    || warn "push failed; entrypoint will retry on container start"
else
  log "nothing new to commit"
fi

# Final chown: every git op above ran as root, leaving root-owned files. The
# container's agent runs as UID 1001 and must own the entire tree.
chown -R 1001:1001 "$VOLUME_PATH"

# ── 5. INSERT into Postgres ─────────────────────────────────────────────────
log "inserting tenants row"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<SQL
insert into tenants (
  id, telegram_user_id, telegram_username, name, country, timezone,
  state_repo_url, volume_path, image_version, internal_port,
  container_name, status
) values (
  '${TENANT_UUID}', ${TELEGRAM_USER_ID}, $(printf "'%s'" "${USERNAME:-NULL}" | sed "s/'NULL'/NULL/"),
  '${NAME}', '${COUNTRY}', '${TIMEZONE}',
  '${REPO_URL}', '${VOLUME_PATH}', '${IMAGE_TAG}', ${INTERNAL_PORT},
  '${CONTAINER_NAME}', 'active'
)
on conflict (telegram_user_id) do update set
  state_repo_url   = excluded.state_repo_url,
  volume_path      = excluded.volume_path,
  internal_port    = excluded.internal_port,
  container_name   = excluded.container_name,
  image_version    = excluded.image_version,
  status           = 'active',
  last_active_at   = now();
SQL

# ── 6. Create the container (don't start; router will) ──────────────────────
log "creating docker container ${CONTAINER_NAME}"
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
docker create \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --memory=1g \
  --memory-swap=1g \
  --memory-reservation=400m \
  --cpus=0.5 \
  --pids-limit=200 \
  --ulimit nofile=2048:4096 \
  --ulimit nproc=512:1024 \
  --security-opt no-new-privileges \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  -p "127.0.0.1:${INTERNAL_PORT}:8787" \
  -v "${VOLUME_PATH}:/home/agent/state" \
  -e TENANT_ID="$TENANT_UUID" \
  -e TENANT_REPO_URL="$REPO_URL" \
  -e GITHUB_PAT="$GITHUB_PAT" \
  -e SHARED_ANTHROPIC_KEY="${SHARED_ANTHROPIC_KEY:-}" \
  -e SHARED_OPENAI_KEY="${SHARED_OPENAI_KEY:-}" \
  -e ANTHROPIC_API_KEY="${SHARED_ANTHROPIC_KEY:-}" \
  -e OPENAI_API_KEY="${SHARED_OPENAI_KEY:-}" \
  -e DEEPSEEK_API_KEY="${SHARED_DEEPSEEK_KEY:-}" \
  -e MOONSHOT_API_KEY="${SHARED_MOONSHOT_KEY:-}" \
  -e SHARED_DEEPSEEK_KEY="${SHARED_DEEPSEEK_KEY:-}" \
  -e SHARED_MOONSHOT_KEY="${SHARED_MOONSHOT_KEY:-}" \
  -e ARETE_SKIP_BOOT_GATE="${ARETE_SKIP_BOOT_GATE:-1}" \
  -e FIXTURE_SYSTEM_PROMPT_CAP="${FIXTURE_SYSTEM_PROMPT_CAP:-2048}" \
  -e SUPABASE_DB_URL="$DATABASE_URL" \
  -e TELEGRAM_INTERNAL_PORT=8787 \
  "$IMAGE_TAG" > /dev/null

log "provisioned. tenant_id=${TENANT_UUID}"
log "next: send a Telegram message to the bot — router will start the container on demand"
echo ""
echo "Inspect:"
echo "  psql \"\$DATABASE_URL\" -c \"select * from tenants where telegram_user_id = ${TELEGRAM_USER_ID}\""
echo "  docker ps -a --filter name=${CONTAINER_NAME}"
echo "  docker logs ${CONTAINER_NAME}"
