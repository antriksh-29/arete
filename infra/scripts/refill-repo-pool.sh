#!/usr/bin/env bash
# refill-repo-pool.sh — top up the state-repo pool to the target size.
#
# Q-I from architecture-research: pre-create N blank state repos from
# the arete-state-template so provisioning becomes a ~1s rename instead
# of a 5-8s template-generate. This script:
#
#   1. Counts current free pool repos in Postgres
#   2. Creates (target - current) new ones via GitHub API + state-template
#   3. Inserts pool rows into state_repo_pool
#
# Usage (sourcing /etc/arete-secrets/provisioner.env for GITHUB_PAT):
#   sudo bash infra/scripts/refill-repo-pool.sh 50
#
# Idempotent — running with target=50 when 30 already exist creates 20 more.

set -euo pipefail

TARGET="${1:-50}"

if [ -f /etc/arete-secrets/provisioner.env ]; then
  set -a; . /etc/arete-secrets/provisioner.env; set +a
fi

: "${DATABASE_URL:?DATABASE_URL required (set or source /etc/arete-secrets/router.env)}"
: "${GITHUB_PAT:?GITHUB_PAT required}"
GHCR_USER="${GHCR_USER:-antriksh-29}"
TEMPLATE_REPO="${TEMPLATE_REPO:-arete-state-template}"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'
log()  { echo "${green}[pool]${rst} $*"; }
warn() { echo "${yellow}[pool]${rst} $*"; }

# ── 1. Count current free pool ───────────────────────────────────────────────
CURRENT=$(psql "$DATABASE_URL" -At -c "select count(*) from state_repo_pool where state = 'free'")
log "current free pool: $CURRENT / target $TARGET"

NEEDED=$((TARGET - CURRENT))
if [ "$NEEDED" -le 0 ]; then
  log "pool already at or above target — nothing to do"
  exit 0
fi
log "creating $NEEDED new pool repos"

# ── 2. Create N repos via GitHub template-generate API ───────────────────────
created=0
attempt=0
while [ "$created" -lt "$NEEDED" ] && [ "$attempt" -lt $((NEEDED * 3)) ]; do
  attempt=$((attempt + 1))
  # Short random suffix; collision is rare but check via 422
  SUFFIX=$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 8)
  NAME="arete-pool-${SUFFIX}"

  HTTP=$(curl -sS -o /tmp/repo.json -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${GITHUB_PAT}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${GHCR_USER}/${TEMPLATE_REPO}/generate" \
    -d "{\"owner\":\"${GHCR_USER}\",\"name\":\"${NAME}\",\"private\":true,\"description\":\"Arete state-repo pool — claim before signup\"}")

  if [ "$HTTP" = "201" ]; then
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
      "insert into state_repo_pool(name, owner) values ('${NAME}', '${GHCR_USER}') on conflict (name) do nothing" \
      > /dev/null
    created=$((created + 1))
    log "[$created/$NEEDED] created ${NAME}"
  elif [ "$HTTP" = "422" ] && grep -qi 'name already exists' /tmp/repo.json; then
    warn "name collision on ${NAME}, retrying"
    continue
  else
    warn "github create failed (HTTP ${HTTP}) for ${NAME}: $(head -c 200 /tmp/repo.json)"
    sleep 2
  fi
done

if [ "$created" -lt "$NEEDED" ]; then
  echo "${red}[pool]${rst} only created $created/$NEEDED — check rate limits or token scopes" >&2
  exit 1
fi

log "done. final free pool size: $(psql "$DATABASE_URL" -At -c "select count(*) from state_repo_pool where state = 'free'")"
