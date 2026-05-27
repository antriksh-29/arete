#!/usr/bin/env bash
# install-router.sh — deploy + enable the arete-router systemd service.
#
# Run from a checkout of the platform repo on the host. Idempotent.
#
# Steps:
#   1. Ensure Node 24+ is installed (via NodeSource repo)
#   2. Sync services/router/ → /opt/arete/services/router/
#   3. npm install + npm run build there
#   4. Install systemd unit at /etc/systemd/system/arete-router.service
#   5. Bail with a friendly note if /etc/arete-secrets/router.env doesn't exist
#   6. systemctl enable --now arete-router; show status
#
# Usage:
#   sudo bash infra/scripts/install-router.sh
#
# Expected /etc/arete-secrets/router.env contents (operator must create before
# starting the service; install-router.sh prints a template if missing):
#
#   DATABASE_URL=postgresql://...
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_WEBHOOK_SECRET=...    # optional but recommended
#   ADMIN_API_KEY=...              # optional; enables /admin/* endpoints
#   PORT=8080
#   LOG_LEVEL=info

set -euo pipefail

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'
log()  { echo "${green}[install-router]${rst} $*"; }
warn() { echo "${yellow}[install-router]${rst} $*"; }
die()  { echo "${red}[install-router]${rst} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_DIR=/opt/arete/services/router
UNIT_FILE=/etc/systemd/system/arete-router.service
ENV_FILE=/etc/arete-secrets/router.env

# ── 1. Node 24+ ──────────────────────────────────────────────────────────────
NODE_OK=0
if command -v node >/dev/null; then
  NV="$(node -v | sed 's/^v//' | cut -d. -f1)"
  [ "$NV" -ge 20 ] && NODE_OK=1
fi
if [ $NODE_OK -eq 0 ]; then
  log "installing Node 24 via NodeSource"
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
  apt-get install -y nodejs
fi
log "Node: $(node -v)"

# ── 2. Sync code ─────────────────────────────────────────────────────────────
log "syncing router code to ${INSTALL_DIR}"
install -d -m 0755 -o arete -g arete /opt/arete/services
rsync -a --delete \
  --exclude node_modules --exclude dist --exclude .env \
  "${REPO_ROOT}/services/router/" \
  "${INSTALL_DIR}/"
chown -R arete:arete /opt/arete/services

# ── 3. Build ─────────────────────────────────────────────────────────────────
log "npm install + build"
sudo -u arete bash -c "cd ${INSTALL_DIR} && npm install --omit=dev --no-audit --no-fund && npm install --no-audit --no-fund && npm run build"

# ── 4. Systemd unit ──────────────────────────────────────────────────────────
log "installing systemd unit"
cp "${REPO_ROOT}/infra/services/arete-router.service" "$UNIT_FILE"
systemctl daemon-reload

# ── 5. Env file check ────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  warn "${ENV_FILE} does not exist — service will fail to start"
  warn "create it with:"
  cat <<EOF

  sudo install -d -m 0700 /etc/arete-secrets
  sudo tee ${ENV_FILE} > /dev/null <<'ENV'
DATABASE_URL=postgresql://postgres:PASSWORD@db.<ref>.supabase.co:5432/postgres
TELEGRAM_BOT_TOKEN=<from-1password>
TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 32)
ADMIN_API_KEY=$(openssl rand -hex 32)
PORT=8080
LOG_LEVEL=info
ENV
  sudo chmod 0600 ${ENV_FILE}

EOF
  warn "after creating, run: sudo systemctl restart arete-router"
  exit 0
fi
chmod 0600 "$ENV_FILE"

# ── 6. Enable + start ────────────────────────────────────────────────────────
log "enabling + starting arete-router"
systemctl enable --now arete-router
sleep 2
systemctl status arete-router --no-pager -l | head -20 || true
echo ""
log "done. tail logs:  journalctl -u arete-router -f"
log "health check:    curl -s http://localhost:8080/health"
