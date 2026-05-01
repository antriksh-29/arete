#!/bin/bash
# Daily R2 backup. Tarballs the brain repo + openclaw config to Cloudflare R2.
# 30-day retention via rclone delete --min-age=30d.
#
# What we back up (in priority order):
#   1. ~/.brain/         — markdown pages + git history (the source of truth)
#   2. ~/.gbrain/        — gbrain config (URLs, schema state)
#   3. ~/.openclaw/openclaw.json + identity files (workspace AGENTS.md, etc.)
#
# Skipping: Supabase pgdump. The brain repo IS the source of truth; gbrain
# rebuilds embeddings from .md files via `gbrain sync`. The DB is derivable.

set -e
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

LOG_DIR="${LOG_DIR:-$HOME/.brain-bootstrap-raw/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/backup-$(date +%Y-%m-%d).log"
STAMP=$(date +%Y%m%d-%H%M%S)

echo "[$(date -Iseconds)] backup start" >> "$LOG"

# rclone config from vault on the fly (avoids persistent config file).
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ENDPOINT="$(/usr/local/bin/bot-cred get CLOUDFLARE_R2_ENDPOINT)"
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$(/usr/local/bin/bot-cred get CLOUDFLARE_R2_ACCESS_KEY)"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$(/usr/local/bin/bot-cred get CLOUDFLARE_R2_SECRET_KEY)"
export RCLONE_CONFIG_R2_ACL=private

if [ -z "$RCLONE_CONFIG_R2_ENDPOINT" ] || [ -z "$RCLONE_CONFIG_R2_ACCESS_KEY_ID" ]; then
  echo "[$(date -Iseconds)] ABORT: R2 credentials not in vault" >> "$LOG"
  exit 1
fi

WORK=$(mktemp -d)
trap "rm -rf $WORK" EXIT

# 1. Brain repo (includes .git so we can restore history)
tar -czf "$WORK/brain-$STAMP.tar.gz" -C "$HOME" .brain 2>>"$LOG"
echo "  brain tarball: $(stat -c %s $WORK/brain-$STAMP.tar.gz) bytes" >> "$LOG"

# 2. gbrain + openclaw config (small, no .git, no sessions)
tar -czf "$WORK/config-$STAMP.tar.gz" -C "$HOME" \
  .gbrain/config.json \
  .openclaw/openclaw.json \
  .openclaw/workspace/AGENTS.md \
  .openclaw/workspace/skills 2>>"$LOG"
echo "  config tarball: $(stat -c %s $WORK/config-$STAMP.tar.gz) bytes" >> "$LOG"

# Upload to R2 under daily/ prefix
rclone copy "$WORK/" R2:personal-assistant-backups/daily/ --include "*-$STAMP.tar.gz" 2>>"$LOG"
echo "[$(date -Iseconds)] uploaded to R2:personal-assistant-backups/daily/" >> "$LOG"

# Retention: delete daily/ files older than 30 days
rclone delete R2:personal-assistant-backups/daily/ --min-age 30d 2>>"$LOG" || true
echo "[$(date -Iseconds)] retention sweep done" >> "$LOG"

# Local log retention: keep last 14
ls -t "$LOG_DIR"/backup-*.log 2>/dev/null | tail -n +15 | xargs -r rm

echo "[$(date -Iseconds)] backup done" >> "$LOG"
