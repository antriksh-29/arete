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

# ── 3.5. Install provisioner script + sudoers entry ──────────────────────────
# Router runs as 'arete' user. The provisioner needs root for chown to UID
# 1001, docker create, etc. Sudoers grants ONLY this exact path with
# controlled args, no other sudo capability for the arete user.
log "installing provisioner at /opt/arete/scripts/provision-tenant.sh"
install -d -m 0755 -o root -g root /opt/arete/scripts
install -m 0755 -o root -g root \
  "${REPO_ROOT}/infra/scripts/provision-test-tenant.sh" \
  /opt/arete/scripts/provision-tenant.sh

SUDOERS_FILE=/etc/sudoers.d/arete-router
log "writing sudoers entry at ${SUDOERS_FILE}"
cat > "$SUDOERS_FILE" <<'SUDOERS'
# Allow the arete-router systemd service to run the provisioner without
# password. The path is fixed (/opt/arete/scripts/provision-tenant.sh)
# and the args are controlled by router code (telegram_user_id + optional
# --username). No other sudo capability is granted.
arete ALL=(root) NOPASSWD: /usr/bin/bash /opt/arete/scripts/provision-tenant.sh *
SUDOERS
chmod 0440 "$SUDOERS_FILE"
chown root:root "$SUDOERS_FILE"
# Validate (sudoers syntax errors lock you out — visudo -c catches them)
if ! visudo -cf "$SUDOERS_FILE" > /dev/null; then
  die "sudoers file failed validation: $SUDOERS_FILE"
fi

# ── 3.7. UFW: allow docker bridge → router ──────────────────────────────────
# Tenant containers reach the router via the docker bridge gateway
# (172.17.0.1:8080) to call /internal/reminders/schedule etc.
# Default UFW posture is deny-incoming; without this rule, every
# container→router call times out (silent failure mode — found 2026-05-28).
# Idempotent: ufw allow returns "Skipping adding existing rule" on re-run.
if command -v ufw > /dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  log "adding UFW rule: 172.17.0.0/16 → :8080 (docker bridge → router internal API)"
  ufw allow from 172.17.0.0/16 to any port 8080 proto tcp \
    comment "docker→router internal API" > /dev/null
fi

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

# ── 6. Enable + restart ──────────────────────────────────────────────────────
# `enable --now` only starts if not already running; we need to RESTART so
# new compiled dist/ is loaded into the live process (bit us a few times
# in earlier deploys when fix PRs landed but the running router still had
# the old code in memory).
log "enabling + restarting arete-router"
systemctl enable arete-router
systemctl restart arete-router
sleep 2
systemctl status arete-router --no-pager -l | head -20 || true
echo ""
log "done. tail logs:  journalctl -u arete-router -f"
log "health check:    curl -s http://localhost:8080/health"
