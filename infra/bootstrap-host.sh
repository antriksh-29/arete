#!/usr/bin/env bash
# bootstrap-host.sh — bring a bare Ubuntu/Debian box to "ready to run Arete tenants."
#
# Works in two modes:
#   - AFTER cloud-init: skips already-done steps (idempotent).
#   - On a FRESH box (no cloud-init): self-installs everything from scratch.
#
# Run on the VPS as root (or via sudo). Tested on Ubuntu 24.04 + Debian 12.
#
# Usage:
#   sudo PLATFORM_DOMAIN=arete.example.com GITHUB_PAT=ghp_xxx bash infra/bootstrap-host.sh
#
# Or with no domain (DNS not ready — skips Caddy install):
#   sudo GITHUB_PAT=ghp_xxx bash infra/bootstrap-host.sh
#
# Steps:
#   1. apt update + base packages (curl, ufw, fail2ban, git, jq, etc.)
#   2. Docker Engine + compose plugin (from official Docker repo)
#   3. `arete` user (sudo + docker group)
#   4. UFW (22, 80, 443 in; deny rest)
#   5. fail2ban
#   6. 4 GB swap if missing
#   7. Skeleton dirs (/var/tenants, /var/lib/arete-creds, /etc/arete-secrets, /var/log/arete)
#   8. Caddy + render Caddyfile (only if PLATFORM_DOMAIN is set)
#   9. Docker login to GHCR + image pull (only if GITHUB_PAT is set)
#   10. Generate /etc/arete-secrets/master.key if missing

set -euo pipefail

PLATFORM_DOMAIN="${PLATFORM_DOMAIN:-}"
GITHUB_PAT="${GITHUB_PAT:-}"
GHCR_USER="${GHCR_USER:-antriksh-29}"
IMAGE="ghcr.io/antriksh-29/arete:latest"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'

log()  { echo "${green}[bootstrap]${rst} $*"; }
warn() { echo "${yellow}[bootstrap]${rst} $*"; }
die()  { echo "${red}[bootstrap]${rst} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"

# ── 1. Base packages ─────────────────────────────────────────────────────────
log "step 1/10: apt update + base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl gnupg git jq htop rsync unzip \
  ufw fail2ban unattended-upgrades

# ── 2. Docker ────────────────────────────────────────────────────────────────
if command -v docker >/dev/null && docker --version | grep -qE 'Docker version'; then
  log "step 2/10: Docker already installed — $(docker --version)"
else
  log "step 2/10: installing Docker Engine + compose plugin"
  install -m 0755 -d /etc/apt/keyrings

  # Detect Ubuntu vs Debian for the right repo
  . /etc/os-release
  case "$ID" in
    ubuntu) DOCKER_REPO_URL="https://download.docker.com/linux/ubuntu" ;;
    debian) DOCKER_REPO_URL="https://download.docker.com/linux/debian" ;;
    *)      die "unsupported OS: $ID (need ubuntu or debian)" ;;
  esac

  curl -fsSL "$DOCKER_REPO_URL/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] $DOCKER_REPO_URL $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  # Sensible defaults: log rotation, live-restore
  cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  },
  "live-restore": true
}
EOF
  systemctl restart docker || true
  systemctl enable --now docker
  log "Docker installed: $(docker --version)"
fi

# ── 3. arete user ────────────────────────────────────────────────────────────
if id arete >/dev/null 2>&1; then
  log "step 3/10: user 'arete' exists"
  usermod -aG docker arete || true
else
  log "step 3/10: creating user 'arete'"
  useradd --create-home --shell /bin/bash --groups sudo,docker arete
  echo 'arete ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/arete
  chmod 0440 /etc/sudoers.d/arete

  # Mirror the operator's authorized SSH keys (so `ssh arete@<ip>` works too)
  if [ -d /root/.ssh ] && [ -f /root/.ssh/authorized_keys ]; then
    install -d -m 0700 -o arete -g arete /home/arete/.ssh
    install -m 0600 -o arete -g arete /root/.ssh/authorized_keys /home/arete/.ssh/authorized_keys
    log "mirrored root's authorized_keys to arete user"
  fi
fi

# ── 4. UFW ───────────────────────────────────────────────────────────────────
if ufw status | grep -q "Status: active"; then
  log "step 4/10: UFW already active"
else
  log "step 4/10: configuring UFW"
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

# ── 5. fail2ban ──────────────────────────────────────────────────────────────
log "step 5/10: fail2ban"
systemctl enable --now fail2ban

# ── 6. Swap ──────────────────────────────────────────────────────────────────
if [ -f /swapfile ] || swapon --show | grep -q '^'; then
  log "step 6/10: swap already configured"
else
  log "step 6/10: creating 4G swap"
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  cat > /etc/sysctl.d/99-arete.conf <<'EOF'
vm.swappiness=10
net.core.somaxconn=4096
net.ipv4.tcp_fin_timeout=15
EOF
  sysctl --system >/dev/null
fi

# ── 7. Skeleton dirs ─────────────────────────────────────────────────────────
log "step 7/10: skeleton dirs"
install -d -m 0755 -o arete -g arete /var/tenants
install -d -m 0700 -o arete -g arete /var/lib/arete-creds
install -d -m 0700 -o root  -g root  /etc/arete-secrets
install -d -m 0755 -o arete -g arete /var/log/arete

# ── 8. Caddy + TLS ───────────────────────────────────────────────────────────
if [ -n "$PLATFORM_DOMAIN" ]; then
  if ! command -v caddy >/dev/null; then
    log "step 8/10: installing Caddy"
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
  else
    log "step 8/10: Caddy already installed"
  fi

  log "rendering Caddyfile for ${PLATFORM_DOMAIN}"
  if [ -f "$SCRIPT_DIR/Caddyfile.template" ]; then
    sed "s/\${PLATFORM_DOMAIN}/${PLATFORM_DOMAIN}/g" "$SCRIPT_DIR/Caddyfile.template" > /etc/caddy/Caddyfile
  else
    die "Caddyfile.template not found at $SCRIPT_DIR/Caddyfile.template — make sure you cloned the repo with that file present"
  fi
  systemctl enable --now caddy
  systemctl reload caddy
  log "Caddy running. TLS cert will be issued on first request to ${PLATFORM_DOMAIN}"
else
  warn "step 8/10: PLATFORM_DOMAIN unset — skipping Caddy install (re-run with PLATFORM_DOMAIN=… once DNS is pointed)"
fi

# ── 9. GHCR login + image pull ──────────────────────────────────────────────
if [ -n "$GITHUB_PAT" ]; then
  log "step 9/10: logging into GHCR as ${GHCR_USER}"
  echo "$GITHUB_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
  log "pulling ${IMAGE} (warming cache for first tenant)"
  docker pull "$IMAGE" || warn "image pull failed — check PAT scopes (needs read:packages) or wait for first CI build"
else
  warn "step 9/10: GITHUB_PAT unset — skipping GHCR login. Image pull will fail until you docker login manually."
fi

# ── 10. Vault master key ─────────────────────────────────────────────────────
MASTER_KEY=/etc/arete-secrets/master.key
if [ ! -f "$MASTER_KEY" ]; then
  log "step 10/10: generating vault master key at ${MASTER_KEY}"
  install -d -m 0700 -o root -g root /etc/arete-secrets
  openssl rand -hex 32 > "$MASTER_KEY"
  chmod 0400 "$MASTER_KEY"
  chown root:root "$MASTER_KEY"
else
  log "step 10/10: vault master key already present at ${MASTER_KEY}"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
log "host bootstrap complete"
echo ""
echo "${green}=== Host ready ===${rst}"
echo "  Host IP:    $(hostname -I | awk '{print $1}')"
echo "  Docker:     $(docker --version)"
echo "  arete user: $(id arete | head -c 80)"
[ -n "$PLATFORM_DOMAIN" ] && echo "  Caddy:      $(systemctl is-active caddy) (managing ${PLATFORM_DOMAIN})"
[ -n "$GITHUB_PAT" ] && echo "  Image:      $(docker images --format '{{.Repository}}:{{.Tag}} ({{.Size}})' ghcr.io/antriksh-29/arete:latest 2>/dev/null || echo 'not yet pulled')"
echo ""
echo "Next:"
echo "  1. Confirm DNS points at this box:  dig +short ${PLATFORM_DOMAIN:-<your-domain>}"
[ -z "$PLATFORM_DOMAIN" ] && echo "  2. Re-run with PLATFORM_DOMAIN=… to install Caddy + provision TLS"
echo "  3. Phase C will land the router/provisioner systemd units. Until then,"
echo "     the box is bootstrapped but no tenants are running yet."
