#!/usr/bin/env bash
# bootstrap-host.sh — finalize Arete host setup AFTER cloud-init.
#
# Run on the VPS as user `arete` (or root with sudo). Idempotent.
#
# What it does:
#   1. Validates cloud-init prereqs (docker, ufw, arete user exist).
#   2. Installs Caddy from official repo + provisions a Let's Encrypt cert for
#      $PLATFORM_DOMAIN. SKIPPED if PLATFORM_DOMAIN is unset (e.g. first run
#      before DNS is pointed).
#   3. Authenticates Docker against GHCR using $GITHUB_PAT so the host can pull
#      the private Arete image.
#   4. Pulls the latest `arete:latest` image (warm cache for first tenant).
#   5. Sets up /etc/arete-secrets/master.key (vault encryption key — randomly
#      generated, 0600, root:root). Skipped if already present.
#   6. Prints a summary of next steps (start the router service — Phase C).
#
# Usage:
#   sudo PLATFORM_DOMAIN=arete.example.com GITHUB_PAT=ghp_xxx bash infra/bootstrap-host.sh
#
# Or with no DOMAIN (DNS not ready yet — skips Caddy):
#   sudo GITHUB_PAT=ghp_xxx bash infra/bootstrap-host.sh

set -euo pipefail

PLATFORM_DOMAIN="${PLATFORM_DOMAIN:-}"
GITHUB_PAT="${GITHUB_PAT:-}"
GHCR_USER="${GHCR_USER:-antriksh-29}"
IMAGE="ghcr.io/antriksh-29/arete:latest"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'

log()  { echo "${green}[bootstrap]${rst} $*"; }
warn() { echo "${yellow}[bootstrap]${rst} $*"; }
die()  { echo "${red}[bootstrap]${rst} $*" >&2; exit 1; }

# ── 1. Prereq sanity ─────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"
command -v docker >/dev/null || die "docker not found — cloud-init didn't finish"
id arete >/dev/null 2>&1 || die "user 'arete' not found — cloud-init didn't finish"
[ -d /var/tenants ] || die "/var/tenants missing — cloud-init didn't finish"

log "prereqs OK"

# ── 2. Caddy + TLS ───────────────────────────────────────────────────────────
if [ -n "$PLATFORM_DOMAIN" ]; then
  if ! command -v caddy >/dev/null; then
    log "installing Caddy"
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
  else
    log "Caddy already installed"
  fi

  log "writing Caddyfile for ${PLATFORM_DOMAIN}"
  # Renders infra/Caddyfile.template with the domain substituted.
  if [ -f "$(dirname "$0")/Caddyfile.template" ]; then
    sed "s/\${PLATFORM_DOMAIN}/${PLATFORM_DOMAIN}/g" "$(dirname "$0")/Caddyfile.template" > /etc/caddy/Caddyfile
  else
    cat > /etc/caddy/Caddyfile <<EOF
${PLATFORM_DOMAIN} {
    reverse_proxy localhost:8080 {
        header_up Host {host}
        header_up X-Real-IP {remote_host}
    }
    encode gzip zstd
    log {
        output file /var/log/caddy/access.log {
            roll_size 50mb
            roll_keep 5
        }
    }
}
EOF
  fi
  systemctl enable --now caddy
  systemctl reload caddy
  log "Caddy running. TLS cert will be issued on first request to ${PLATFORM_DOMAIN}"
else
  warn "PLATFORM_DOMAIN unset — skipping Caddy install. Re-run with PLATFORM_DOMAIN=… once DNS is pointed."
fi

# ── 3. GHCR login ────────────────────────────────────────────────────────────
if [ -n "$GITHUB_PAT" ]; then
  log "logging into GHCR as ${GHCR_USER}"
  echo "$GITHUB_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
else
  warn "GITHUB_PAT unset — skipping GHCR login. Image pull will fail until you docker login manually."
fi

# ── 4. Pull image (warm cache) ──────────────────────────────────────────────
if docker info >/dev/null 2>&1; then
  log "pulling ${IMAGE} (warming cache for first tenant)"
  docker pull "$IMAGE" || warn "image pull failed — check PAT scopes or wait for first CI build to complete"
fi

# ── 5. Vault master key ──────────────────────────────────────────────────────
MASTER_KEY=/etc/arete-secrets/master.key
if [ ! -f "$MASTER_KEY" ]; then
  log "generating vault master key at ${MASTER_KEY}"
  install -d -m 0700 -o root -g root /etc/arete-secrets
  openssl rand -hex 32 > "$MASTER_KEY"
  chmod 0400 "$MASTER_KEY"
  chown root:root "$MASTER_KEY"
else
  log "vault master key already present at ${MASTER_KEY}"
fi

# ── 6. Summary ───────────────────────────────────────────────────────────────
log "host bootstrap complete"
echo ""
echo "Next steps:"
echo "  1. Confirm DNS:  dig +short ${PLATFORM_DOMAIN:-<your-domain>} → should resolve to this box"
echo "  2. Test image:   docker run --rm ${IMAGE} echo 'hello from arete'"
echo "  3. Phase C will land the router/provisioner systemd units. Until then,"
echo "     the box is bootstrapped but no tenants are running yet."
echo ""
echo "Host IP: $(hostname -I | awk '{print $1}')"
echo "Docker:  $(docker --version)"
[ -n "$PLATFORM_DOMAIN" ] && echo "Caddy:   $(systemctl is-active caddy) (managing ${PLATFORM_DOMAIN})"
