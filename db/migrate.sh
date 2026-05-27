#!/usr/bin/env bash
# db/migrate.sh — apply every .sql in db/migrations/ that hasn't been applied yet.
#
# Idempotent. Uses the schema_migrations table to track applied versions.
#
# Usage:
#   DATABASE_URL=postgresql://... bash db/migrate.sh           # apply pending
#   DATABASE_URL=postgresql://... bash db/migrate.sh --status  # list status only
#   DATABASE_URL=postgresql://... bash db/migrate.sh --dry-run # show what would run

set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set (use 1Password: ARETE_SUPABASE → db_url)}"

cd "$(dirname "$0")"

MODE="${1:-apply}"

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; rst=$'\033[0m'
log()  { echo "${green}[migrate]${rst} $*"; }
warn() { echo "${yellow}[migrate]${rst} $*"; }
die()  { echo "${red}[migrate]${rst} $*" >&2; exit 1; }

command -v psql >/dev/null || die "psql not found — apt install postgresql-client"

# Ensure schema_migrations exists (bootstrap chicken-and-egg)
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q <<'EOF' >/dev/null
create table if not exists schema_migrations (
  version     text primary key,
  applied_at  timestamptz not null default now()
);
EOF

applied=$(psql "$DATABASE_URL" -At -c "select version from schema_migrations order by version")

for f in migrations/*.sql; do
  v="$(basename "$f" .sql)"
  if echo "$applied" | grep -qx "$v"; then
    case "$MODE" in
      --status|--dry-run) echo "  ✓ $v (applied)" ;;
    esac
    continue
  fi

  case "$MODE" in
    --status)  echo "  ✗ $v (pending)" ;;
    --dry-run) echo "  → would apply: $v" ;;
    apply)
      log "applying $v"
      psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f "$f"
    ;;
  esac
done

if [ "$MODE" = "apply" ]; then
  log "done. Current state:"
  psql "$DATABASE_URL" -c "select version, applied_at from schema_migrations order by version"
fi
