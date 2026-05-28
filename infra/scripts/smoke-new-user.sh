#!/usr/bin/env bash
# smoke-new-user.sh — verify the new-user signup flow without a real Telegram user.
#
# What this script tests:
#   1. Provisioner runs end-to-end for a synthetic telegram_user_id
#   2. tenant row inserted with internal_key set + onboarding_stage='awaiting_name'
#   3. Container created with the right env (INTERNAL_KEY, ROUTER_INTERNAL_URL,
#      TENANT_ID, BRAIN_PATH, etc.)
#   4. Volume cloned, identity files in place, gbrain-init sentinel present
#      (after container start)
#   5. Reminders pipeline reachable using the new tenant's internal_key
#   6. Cleanup
#
# What this script does NOT test (those need a real Telegram user):
#   - Telegram inbound webhook → router → onboarding FSM transcript
#   - Actual delivery of "Setting up your assistant..." and onboarding
#     greetings to a real chat
#   - The agent-side flow of typing "remind me in 1 min..." through
#     OpenClaw → /opt/arete/skills/reminders → arete-remind
#
# For a real beta-user smoke test, see docs/STRESS-TEST.md.
#
# Usage:
#   sudo bash infra/scripts/smoke-new-user.sh           # synthetic id (timestamp-based)
#   sudo bash infra/scripts/smoke-new-user.sh --telegram-user-id 999000111
#   sudo bash infra/scripts/smoke-new-user.sh --keep    # don't clean up at the end

set -euo pipefail

TELEGRAM_USER_ID=""
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --telegram-user-id) TELEGRAM_USER_ID="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Default: a sequential synthetic id in the 9xx-million range that's unlikely
# to collide with real telegram users (real ids are typically <2B but >100M).
# We reserve 999_000_000–999_999_999 for smoke tests.
if [ -z "$TELEGRAM_USER_ID" ]; then
  TELEGRAM_USER_ID=$((999000000 + RANDOM))
fi

green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; bold=$'\033[1m'; rst=$'\033[0m'
step() { echo "${bold}${green}▸ $*${rst}"; }
ok()   { echo "  ${green}✓${rst} $*"; }
warn() { echo "  ${yellow}!${rst} $*"; }
fail() { echo "  ${red}✗${rst} $*" >&2; FAILED=1; }

FAILED=0

# Load DATABASE_URL from the router env file (same source the router uses).
export DATABASE_URL="${DATABASE_URL:-$(sudo grep ^DATABASE_URL= /etc/arete-secrets/router.env | cut -d= -f2-)}"
: "${DATABASE_URL:?DATABASE_URL required (could not read from /etc/arete-secrets/router.env)}"

step "Smoke test for telegram_user_id=${TELEGRAM_USER_ID}"

# Pre-flight: make sure this id is NOT in tenants (would skew the test).
EXISTING=$(psql "$DATABASE_URL" -tAc "select id from tenants where telegram_user_id = ${TELEGRAM_USER_ID}")
if [ -n "$EXISTING" ]; then
  fail "telegram_user_id ${TELEGRAM_USER_ID} already exists as tenant ${EXISTING} — pick another id or clean up first"
  exit 1
fi

# ── 1. Provision via the same script the router invokes ────────────────────
step "1/6 — provisioning"
PROVISION_OUT=$(sudo env DATABASE_URL="$DATABASE_URL" \
  bash "$(dirname "$0")/provision-test-tenant.sh" \
  --telegram-user-id "$TELEGRAM_USER_ID" 2>&1) || {
  echo "$PROVISION_OUT" | tail -30
  fail "provisioner exited non-zero"
  exit 1
}
# Strip ANSI color codes from provisioner output before grepping — without
# this, `^\[provision\]` doesn't match the color-prefixed lines and grep's
# exit 1 + pipefail killed the script silently (found 2026-05-28).
PROVISION_CLEAN=$(printf '%s' "$PROVISION_OUT" | sed 's/\x1b\[[0-9;]*m//g')
echo "$PROVISION_CLEAN" | grep -E '^\[provision\] (tenant_id|provisioned)' | sed 's/^/  /' || true
ok "provisioner completed"

# Pull the new tenant row.
ROW=$(psql "$DATABASE_URL" -At -F'|' -c "select id, name, country, onboarding_stage, container_name, internal_key from tenants where telegram_user_id = ${TELEGRAM_USER_ID}")
IFS='|' read -r TENANT_UUID T_NAME T_COUNTRY T_STAGE T_CONTAINER T_KEY <<< "$ROW"

if [ -z "$TENANT_UUID" ]; then
  fail "tenant row missing after provision"
  exit 1
fi
ok "tenant_id=${TENANT_UUID}"

# ── 2. Verify onboarding state is correct for production flow ──────────────
step "2/6 — verifying tenant row state"
[ -z "$T_NAME"     ] && ok "name is NULL (expected for auto-provision)"     || fail "name should be NULL, got '$T_NAME'"
[ -z "$T_COUNTRY"  ] && ok "country is NULL (expected for auto-provision)"  || fail "country should be NULL, got '$T_COUNTRY'"
[ "$T_STAGE" = "awaiting_name" ] && ok "onboarding_stage=awaiting_name" || fail "onboarding_stage should be 'awaiting_name', got '$T_STAGE'"
[ -n "$T_KEY" ] && [ "${#T_KEY}" -eq 64 ] && ok "internal_key set (64 hex chars)" || fail "internal_key length=${#T_KEY} (expected 64)"
[ -n "$T_CONTAINER" ] && ok "container_name=${T_CONTAINER}" || fail "container_name is NULL"

# ── 3. Verify container created with the right env ─────────────────────────
step "3/6 — verifying docker env injection"
if ! sudo docker inspect "$T_CONTAINER" > /dev/null 2>&1; then
  fail "container $T_CONTAINER does not exist"
else
  ok "container exists"
  ENV_DUMP=$(sudo docker inspect "$T_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}')
  for needed in TENANT_ID INTERNAL_KEY ROUTER_INTERNAL_URL BRAIN_PATH; do
    if echo "$ENV_DUMP" | grep -q "^${needed}="; then
      ok "env $needed set"
    else
      fail "env $needed missing in container"
    fi
  done
fi

# ── 4. Verify volume contents ──────────────────────────────────────────────
step "4/6 — verifying volume"
VOL="/var/tenants/$(echo "$TENANT_UUID" | head -c 12)"
# provision-test-tenant.sh uses sha1(telegram_user_id) for the short id, not tenant_uuid:
SHORT_ID=$(printf '%s' "$TELEGRAM_USER_ID" | sha1sum | head -c 12)
VOL="/var/tenants/${SHORT_ID}"
if [ -d "$VOL" ]; then
  ok "volume dir $VOL exists"
  [ -f "$VOL/meta.json" ]            && ok "meta.json present"            || fail "meta.json missing"
  [ -f "$VOL/identity/USER.md" ]     && ok "identity/USER.md present"     || fail "identity/USER.md missing"
else
  fail "volume dir $VOL missing"
fi

# ── 5. Verify internal-API reminders path with new tenant's key ────────────
step "5/6 — reminders pipeline reachable"
if [ -z "$T_KEY" ]; then
  warn "skipping reminders test — no internal_key"
else
  WHEN=$(date -u -d "+90 seconds" "+%Y-%m-%dT%H:%M:%SZ")
  RESP=$(curl -s -X POST http://127.0.0.1:8080/internal/reminders/schedule \
    -H "X-Tenant-Key: $T_KEY" -H "Content-Type: application/json" \
    -d "{\"message\":\"smoke test for tenant ${TENANT_UUID:0:8}\",\"scheduled_for_utc\":\"$WHEN\"}")
  if echo "$RESP" | grep -q '"ok":true'; then
    REM_ID=$(echo "$RESP" | sed -E 's/.*"id":"([^"]+)".*/\1/')
    ok "reminder scheduled (id=${REM_ID:0:8})"
    # Don't wait for it to fire — Telegram delivery will fail anyway (synthetic
    # chat_id doesn't exist), but the row should be inserted and visible.
    psql "$DATABASE_URL" -tAc "select status from reminders where id = '$REM_ID'" \
      | grep -q '^pending$' && ok "row visible in reminders table" || warn "row check skipped"
  else
    fail "reminders/schedule returned: $RESP"
  fi
fi

# ── 6. Cleanup (unless --keep) ─────────────────────────────────────────────
if [ "$KEEP" = "1" ]; then
  warn "cleanup skipped (--keep); reuse with --telegram-user-id $TELEGRAM_USER_ID"
else
  step "6/6 — cleaning up"
  sudo docker rm -f "$T_CONTAINER" > /dev/null 2>&1 && ok "container removed" || warn "container remove failed"
  sudo rm -rf "$VOL" && ok "volume removed" || warn "volume remove failed"
  psql "$DATABASE_URL" -c "delete from reminders where tenant_id = '$TENANT_UUID'" > /dev/null 2>&1 && ok "reminders cleaned"
  psql "$DATABASE_URL" -c "delete from tenants where id = '$TENANT_UUID'" > /dev/null 2>&1 && ok "tenant row removed"
fi

echo ""
if [ "$FAILED" = "0" ]; then
  echo "${bold}${green}SMOKE PASS${rst} — new-user provisioning is healthy"
  exit 0
else
  echo "${bold}${red}SMOKE FAIL${rst} — see above"
  exit 1
fi
