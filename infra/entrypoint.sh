#!/usr/bin/env bash
# /opt/arete/infra/entrypoint.sh — runs on container start.
#
# Lifecycle:
#   1. Validate required env (TENANT_ID, TENANT_REPO_URL, GITHUB_PAT, LLM keys).
#   2. Clone (or pull) the tenant's GitHub state repo into $STATE_HOME.
#   3. Render any one-time templates against tenant metadata.
#   4. Run the injection-fixture smoke test (boot gate — refuses to start if any prompt-injection
#      pattern would succeed; mirrors Alfred's safety scaffolding).
#   5. Start the syncer in the background (batches state/ changes per Telegram turn).
#   6. Start OpenClaw daemon (foreground; tini reaps it on SIGTERM for graceful hibernation).

set -euo pipefail

# Belt-and-braces: even though the image sets safe.directory='*' system-wide,
# add it per-user in case /etc/gitconfig isn't picked up. Idempotent.
git config --global --add safe.directory '*' || true

# --- 1. Validate required env ---
: "${TENANT_ID:?TENANT_ID is required}"
: "${TENANT_REPO_URL:?TENANT_REPO_URL is required (e.g. https://github.com/antriksh-29/arete-acme-assistant.git)}"
: "${GITHUB_PAT:?GITHUB_PAT is required}"
: "${SHARED_ANTHROPIC_KEY:?SHARED_ANTHROPIC_KEY is required}"
: "${SUPABASE_DB_URL:?SUPABASE_DB_URL is required}"
: "${STATE_HOME:?STATE_HOME is required}"
: "${BRAIN_PATH:?BRAIN_PATH is required}"

TELEGRAM_INTERNAL_PORT="${TELEGRAM_INTERNAL_PORT:-8787}"

# Pin the boot-gate's injection-fixture runner to Haiku. Defaults to Sonnet
# upstream — 30 API calls per container start is ~5× more expensive than Haiku
# for no meaningful safety gain on this fixture (the patterns are easy refusals).
# Operators can override by setting INJECTION_MODEL=<id> at docker create time.
export INJECTION_MODEL="${INJECTION_MODEL:-claude-haiku-4-5}"

echo "[arete] starting tenant=${TENANT_ID}"

# --- 2. Clone or pull the tenant state repo ---
AUTH_REPO_URL="$(echo "${TENANT_REPO_URL}" | sed "s|https://|https://x-access-token:${GITHUB_PAT}@|")"

if [ -d "${STATE_HOME}/.git" ]; then
  echo "[arete] state repo present — pulling latest"
  git -C "${STATE_HOME}" remote set-url origin "${AUTH_REPO_URL}"
  # --autostash only handles TRACKED modifications. The provisioner can leave
  # untracked files (e.g. a freshly-rendered meta.json that the template also
  # ships), and those collide with the merge. Stash --include-untracked first,
  # then pop after the pull so the working tree is preserved.
  if [ -n "$(git -C "${STATE_HOME}" status --porcelain)" ]; then
    git -C "${STATE_HOME}" stash push --include-untracked -m "entrypoint-autostash-$(date +%s)" || true
    git -C "${STATE_HOME}" pull --rebase || true
    git -C "${STATE_HOME}" stash pop || true
  else
    git -C "${STATE_HOME}" pull --rebase || true
  fi
else
  echo "[arete] cloning tenant state repo"
  # tmpdir + move so we don't fight an existing volume mount
  TMPDIR="$(mktemp -d)"
  git clone --depth 50 "${AUTH_REPO_URL}" "${TMPDIR}/repo"
  shopt -s dotglob
  mv "${TMPDIR}/repo"/* "${STATE_HOME}/"
  rm -rf "${TMPDIR}"
fi

git -C "${STATE_HOME}" config user.email "arete-bot@arete.local"
git -C "${STATE_HOME}" config user.name "Arete (${TENANT_ID})"

# --- 3. Render one-time templates ---
# meta.json placeholders get filled at first boot if they're still raw.
if grep -q "{{TENANT_ID}}" "${STATE_HOME}/meta.json" 2>/dev/null; then
  IMAGE_VERSION="$(cat /opt/arete/.image-version 2>/dev/null || echo unknown)"
  jq --arg tid "${TENANT_ID}" \
     --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
     --arg iv "${IMAGE_VERSION}" \
     '.tenant_id = $tid | .created_at = $ts | .image_version = $iv' \
     "${STATE_HOME}/meta.json" > "${STATE_HOME}/meta.json.tmp"
  mv "${STATE_HOME}/meta.json.tmp" "${STATE_HOME}/meta.json"
fi

# --- 4. Injection-fixture smoke test (boot gate) ---
# Mirrors Alfred's pattern: refuse to start if any prompt-injection pattern would succeed.
if ! python3 /opt/arete/eval/run-injection-fixture.py --fixture /opt/arete/eval/injection-fixture.yaml; then
  echo "[arete] FATAL: injection fixture refused — container will NOT start" >&2
  exit 1
fi

# --- 5. Start the syncer in background ---
# Watches $STATE_HOME for changes; debounces per Telegram turn; commits + pushes.
nohup /opt/arete/infra/syncer.sh > /tmp/syncer.log 2>&1 &
echo "[arete] syncer started (pid $!)"

# --- 6. Start OpenClaw Gateway (foreground) ---
# Reality check on the OpenClaw CLI (openclaw@2026.5.22):
#   - `openclaw daemon` is a SERVICE-MANAGEMENT subcommand group (systemd
#     install/start/stop), NOT a "run a daemon" command. The previous invocation
#     `openclaw daemon --workspace ... --channel internal-webhook ...` was speculative
#     and silently exited.
#   - The real "run a foreground daemon" command is `openclaw gateway run`. It
#     starts a WebSocket Gateway that subsequent CLI commands (`openclaw agent`,
#     `openclaw skills`, etc.) connect to.
#   - We DO NOT add a Telegram channel inside the container. The router owns the
#     shared bot and proxies user turns via `docker exec ... openclaw agent`. So
#     the container's gateway only needs to accept loopback connections.
#
# OpenClaw expects ANTHROPIC_API_KEY (standard) — alias from our SHARED_ANTHROPIC_KEY.
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-${SHARED_ANTHROPIC_KEY}}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-${SHARED_OPENAI_KEY:-}}"

# Create baseline OpenClaw config if it doesn't exist yet. --non-interactive
# skips prompts; --workspace points at the tenant state dir. Idempotent so it
# safely re-runs on every container start. Best-effort: --allow-unconfigured
# below tolerates a missing or partial config.
openclaw setup --non-interactive --workspace "${STATE_HOME}" || \
  echo "[arete] openclaw setup returned non-zero (continuing — gateway runs with --allow-unconfigured)"

# OpenClaw defaults the gateway port to 18789. The `openclaw gateway health` /
# `openclaw agent` clients also default to that port when reading config — so
# using anything else means we'd have to write a config file to point them at
# the custom port. Just use the default; the gateway is loopback-only anyway
# (router talks to it via `docker exec`, not over the network).
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
echo "[arete] starting openclaw gateway on :${OPENCLAW_GATEWAY_PORT}"
exec openclaw gateway run \
  --port "${OPENCLAW_GATEWAY_PORT}" \
  --bind loopback \
  --auth none \
  --allow-unconfigured \
  --force
