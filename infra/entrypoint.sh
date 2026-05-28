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
# COST: each run = 30 LLM calls × ~2-20KB context per call. Default-skipped because the
# fixture+model+identity tuple is identical across container starts — the gate's value
# is at image-build/release time, not per-container-cold-start. Set ARETE_SKIP_BOOT_GATE=0
# in /etc/arete-secrets/router.env (router-side, propagated via docker create) to enable.
if [ "${ARETE_SKIP_BOOT_GATE:-1}" = "1" ]; then
  echo "[arete] boot gate SKIPPED (ARETE_SKIP_BOOT_GATE=1). Set =0 to enable."
else
  if ! python3 /opt/arete/eval/run-injection-fixture.py --fixture /opt/arete/eval/injection-fixture.yaml; then
    echo "[arete] FATAL: injection fixture refused — container will NOT start" >&2
    exit 1
  fi
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
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-${SHARED_ANTHROPIC_KEY:-}}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-${SHARED_OPENAI_KEY:-}}"
# DeepSeek + Moonshot keys (we route DeepSeek primary, Kimi fallback per
# the OpenClaw native fallback chain — see docs/architecture-research-2026-05-27.md).
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-${SHARED_DEEPSEEK_KEY:-}}"
export MOONSHOT_API_KEY="${MOONSHOT_API_KEY:-${SHARED_MOONSHOT_KEY:-}}"

# Pre-create the state-dir tree so OpenClaw doesn't create it in legacy
# locations. Critical for the OPENCLAW_HOME leak fix (issue #37457).
mkdir -p "${OPENCLAW_HOME}" "${OPENCLAW_STATE_DIR}"

# Create baseline OpenClaw config if it doesn't exist yet. Idempotent.
openclaw onboard --non-interactive --accept-risk --workspace "${STATE_HOME}" || \
  echo "[arete] openclaw onboard returned non-zero (continuing — gateway runs with --allow-unconfigured)"

# --- Configure model routing: DeepSeek primary, Kimi/Haiku/GPT-nano fallbacks ---
# Per architecture-research-2026-05-27.md:
#   - DeepSeek V4 Pro native provider added in openclaw 2026.5.x (no proxy needed)
#   - Pricing $0.435/M input, $0.87/M output (permanent since 2026-05-22)
#   - OpenClaw's native model.fallbacks auto-retries on 429/503/network errors
#   - ~7x cheaper than Haiku 4.5 for the same workload
#
# Only configure providers we have keys for. The fallbacks chain is filtered
# at the OpenClaw layer to skip providers without auth.
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  PRIMARY_MODEL="deepseek/deepseek-v4-pro"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  PRIMARY_MODEL="anthropic/claude-haiku-4-5"
else
  echo "[arete] FATAL: no model-provider API key set (DEEPSEEK_API_KEY or ANTHROPIC_API_KEY required)" >&2
  exit 1
fi

# Build fallback list — only providers we have keys for.
FALLBACKS_JSON='[]'
for entry in 'MOONSHOT_API_KEY|moonshotai/kimi-k2.6' \
             'ANTHROPIC_API_KEY|anthropic/claude-haiku-4-5' \
             'OPENAI_API_KEY|openai/gpt-5.4-nano'; do
  key="${entry%%|*}"
  model="${entry##*|}"
  if [ -n "$(eval echo \$$key)" ] && [ "$model" != "$PRIMARY_MODEL" ]; then
    FALLBACKS_JSON=$(echo "$FALLBACKS_JSON" | jq --arg m "$model" '. + [$m]')
  fi
done
echo "[arete] model routing: primary=$PRIMARY_MODEL fallbacks=$FALLBACKS_JSON"

# Apply via openclaw config patch (atomic, schema-validated, idempotent).
# Per research: this is the canonical way to mutate openclaw.json.
PATCH_JSON=$(jq -n \
  --arg p "$PRIMARY_MODEL" \
  --argjson fb "$FALLBACKS_JSON" \
  --arg ws "$STATE_HOME" \
  '{
    agents: { defaults: { workspace: $ws, model: { primary: $p, fallbacks: $fb } } },
    gateway: { mode: "local", port: 18789, bind: "loopback", auth: { mode: "none" } }
  }')
echo "$PATCH_JSON" | openclaw config patch --stdin --strict-json 2>&1 | head -5 || \
  echo "[arete] openclaw config patch failed (continuing — defaults may apply)"

# Leak check: if despite our env config, openclaw wrote to /root/.openclaw
# or /home/agent/.openclaw, that's the known main-agent bug. Fail-loud here
# rather than running with leaked secrets. Per architecture-research-2026-05-27.md
# (issue #37457).
for legacy in /root/.openclaw /home/agent/.openclaw; do
  if [ -d "$legacy" ]; then
    echo "[arete] FATAL: openclaw wrote to legacy path $legacy despite OPENCLAW_HOME override." >&2
    echo "[arete]   This is the known main-agent leak. Container will NOT start." >&2
    echo "[arete]   Investigate the openclaw version + OPENCLAW_HOME env." >&2
    exit 1
  fi
done

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
