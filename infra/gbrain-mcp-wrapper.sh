#!/usr/bin/env bash
# Wrapper for `gbrain serve` as an OpenClaw MCP child process.
#
# Two reasons this wrapper exists (carried over from the Alfred-era learning,
# but adapted to v2 container context):
#
# 1. OpenClaw spawns MCP children with a stripped env (only PATH/HOME/USER
#    by default). gbrain needs OPENAI_API_KEY to embed queries and
#    DATABASE_URL to talk to Postgres. We re-export them here so the
#    subprocess actually sees them.
#
# 2. The container's PATH at MCP-spawn-time may not include where bun put
#    gbrain. We invoke the system-symlinked binary directly to be safe.

set -euo pipefail

# Re-export the env OpenClaw stripped.
export OPENAI_API_KEY="${OPENAI_API_KEY:-${SHARED_OPENAI_KEY:-}}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-${SHARED_ANTHROPIC_KEY:-}}"
export TENANT_ID="${TENANT_ID:-}"
export BRAIN_PATH="${BRAIN_PATH:-/home/agent/state/brain}"
# DATABASE_URL fed by entrypoint (will include search_path for per-tenant
# schema once PR4 lands; today it's the shared SUPABASE_DB_URL).
export DATABASE_URL="${DATABASE_URL:-${SUPABASE_DB_URL:-}}"

cd "$BRAIN_PATH" 2>/dev/null || {
  echo "[gbrain-wrapper] BRAIN_PATH=${BRAIN_PATH} does not exist; entrypoint should have created it" >&2
  exit 1
}

exec /usr/local/bin/gbrain serve "$@"
