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
# AUTHORITATIVE GATE: .github/workflows/build-image.yml runs the same fixture
# against every release before tagging :latest. The runtime gate here is a
# defense-in-depth opt-in for paranoid operators. By default skipped — running
# 30 LLM calls per container cold-start costs ~$0.30/tenant/wakeup with no
# safety value beyond what CI already proved.
if [ "${ARETE_SKIP_BOOT_GATE:-1}" = "1" ]; then
  echo "[arete] boot gate SKIPPED (default — CI validates the release). Set ARETE_SKIP_BOOT_GATE=0 to opt-in to runtime gate."
else
  echo "[arete] boot gate RUNTIME enabled (ARETE_SKIP_BOOT_GATE=0). 30 LLM calls per container start."
  if ! python3 /opt/arete/eval/run-injection-fixture.py --fixture /opt/arete/eval/injection-fixture.yaml; then
    echo "[arete] FATAL: injection fixture refused — container will NOT start" >&2
    exit 1
  fi
fi

# --- 4.5. Wire platform identity into workspace root ---
# OpenClaw auto-loads AGENTS.md, SOUL.md, USER.md, HEARTBEAT.md, ACCESS_POLICY.md,
# CONFIRMATION_FLOW.md, CONTENT_INGESTION.md, MCP_FIRST.md, MODEL_ROUTING.md,
# BRAIN_FILING.md, BRAIN_PROPAGATION.md, PAGE_SCHEMA.md from workspace root.
# Per docs/architecture-research-2026-05-27.md (Q-C). Files live in the image
# at /opt/arete/identity/. We symlink to workspace root so OpenClaw finds them.
#
# Tenant-rendered USER.md lives at state/identity/USER.md (per provisioner).
# Symlink that to workspace root too, with HIGHER precedence — tenant's USER
# wins over the (empty) platform USER.md template.
#
# v1 doesn't implement the BEGIN PLATFORM block overlay yet; tenant can
# override platform identity files only by replacing them entirely in their
# state repo (rare). See Q-C v2 in research doc.

# Ensure .gitignore excludes the workspace-root symlinks so syncer doesn't
# commit them. Append only if not already present (idempotent).
PLATFORM_IDENTITY_FILES=(
  AGENTS.md SOUL.md IDENTITY.md HEARTBEAT.md ACCESS_POLICY.md CONFIRMATION_FLOW.md
  CONTENT_INGESTION.md MCP_FIRST.md MODEL_ROUTING.md BRAIN_FILING.md
  BRAIN_PROPAGATION.md PAGE_SCHEMA.md USER.md
)
GITIGNORE="${STATE_HOME}/.gitignore"
touch "$GITIGNORE"
if ! grep -q "^# Platform-identity symlinks" "$GITIGNORE"; then
  {
    echo ""
    echo "# Platform-identity symlinks — wired at boot, follow image /opt/arete/identity/."
    echo "# Tenant identity overrides live at identity/AGENTS-overrides.md etc."
    for f in "${PLATFORM_IDENTITY_FILES[@]}"; do echo "/${f}"; done
  } >> "$GITIGNORE"
fi

# Create workspace-root symlinks. AGGRESSIVE override mode: platform owns
# the identity surface; if a tenant repo has a stale AGENTS.md / SOUL.md
# committed (from old OpenClaw bootstrap stubs), we git-rm it and replace
# with our symlink. Tenant overrides live in identity/AGENTS-overrides.md
# etc. and will be merged in v2 (Q-C BEGIN PLATFORM block).
for f in AGENTS.md SOUL.md IDENTITY.md HEARTBEAT.md ACCESS_POLICY.md \
         CONFIRMATION_FLOW.md CONTENT_INGESTION.md MCP_FIRST.md MODEL_ROUTING.md \
         BRAIN_FILING.md BRAIN_PROPAGATION.md PAGE_SCHEMA.md; do
  src="/opt/arete/identity/${f}"
  dst="${STATE_HOME}/${f}"
  if [ ! -f "$src" ]; then continue; fi
  # If file exists and isn't our symlink, blow it away. Also git-rm if tracked.
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    git -C "${STATE_HOME}" -c "safe.directory=*" rm -f --quiet "${f}" 2>/dev/null || rm -f "$dst"
  elif [ -L "$dst" ]; then
    # Existing symlink — refresh in case the source path moved.
    rm -f "$dst"
  fi
  ln -s "$src" "$dst"
done
# Tenant USER.md (rendered by provisioner at identity/USER.md) → workspace
# root. Same aggressive override — workspace-root USER.md is platform-managed
# (the symlink target), so any stale committed copy gets git-rm'd.
if [ -f "${STATE_HOME}/identity/USER.md" ]; then
  if [ -e "${STATE_HOME}/USER.md" ] && [ ! -L "${STATE_HOME}/USER.md" ]; then
    git -C "${STATE_HOME}" -c "safe.directory=*" rm -f --quiet "USER.md" 2>/dev/null || \
      rm -f "${STATE_HOME}/USER.md"
  elif [ -L "${STATE_HOME}/USER.md" ]; then
    rm -f "${STATE_HOME}/USER.md"
  fi
  ln -s "${STATE_HOME}/identity/USER.md" "${STATE_HOME}/USER.md"
fi

echo "[arete] identity wired: $(ls -la "${STATE_HOME}"/AGENTS.md "${STATE_HOME}"/SOUL.md 2>/dev/null | wc -l) symlinks"

# --- 4.6. Wire user-skills/ as workspace skills/ ---
# OpenClaw discovers tenant skills at <workspace>/skills/ (highest precedence).
# State-template uses user-skills/ for tenant-authored skills (the convention
# the spec settled on). Symlink so OpenClaw finds them at the canonical path.
# Platform skills come in via skills.load.extraDirs (set below via openclaw
# config patch); user-skill with same name wins.
if [ -d "${STATE_HOME}/user-skills" ] && [ ! -e "${STATE_HOME}/skills" ]; then
  ln -s "${STATE_HOME}/user-skills" "${STATE_HOME}/skills"
  # Ensure the symlink isn't committed (syncer would otherwise track it).
  if ! grep -q "^/skills$" "$GITIGNORE"; then
    {
      echo ""
      echo "# Workspace skills/ is a symlink to user-skills/ — created at boot."
      echo "/skills"
    } >> "$GITIGNORE"
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

# Skip `openclaw onboard` — it requires a running gateway to validate, which
# we don't have yet (chicken-and-egg). Per docs/architecture-research-2026-05-27.md
# the direct-write path (openclaw config patch) is faster and more
# reproducible than going through onboard's interactive validation.
# We write a minimal openclaw.json via config patch below.

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
    agents: {
      defaults: {
        workspace: $ws,
        model: { primary: $p, fallbacks: $fb },
        # AGENTS.md is ~67KB; per-file cap raised from default 12K to 80K
        # to fit it. Total bumped to 120K to fit AGENTS + SOUL + USER + others.
        bootstrapMaxChars: 80000,
        bootstrapTotalMaxChars: 120000
      },
      list: [{
        id: "main",
        identity: {
          name: "Arete",
          emoji: "🪶"
        }
      }]
    },
    # Skill precedence (highest to lowest):
    #   1. workspace/skills/      - symlink to user-skills/ (tenant overrides)
    #   2. ...                    - OpenClaw built-in paths
    #   6. skills.load.extraDirs  - platform skills from the image
    # A user-skill with the same slug as a platform skill wins. Inspired by
    # the 36-skill bundle from the reference Alfred build (now baked into
    # /opt/arete/skills/ via the image).
    skills: { load: { extraDirs: ["/opt/arete/skills"] } },
    gateway: { mode: "local", port: 18789, bind: "loopback", auth: { mode: "none" } }
  }')
# `openclaw config patch --stdin` reads JSON5 from stdin and writes openclaw.json
# atomically with schema validation. Note: the --strict-json flag from the
# 2026-05-27 research doc DOES NOT EXIST in openclaw@2026.5.22 — real syntax
# is just --stdin. Confirmed via `openclaw config patch --help`.
if ! echo "$PATCH_JSON" | openclaw config patch --stdin 2>&1 | head -5; then
  echo "[arete] FATAL: openclaw config patch failed; gateway would use default OpenAI model with no API key." >&2
  exit 1
fi

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

# Start gateway in BACKGROUND so we can hot-patch the device-pairing
# scope file after first connection. Without this, openclaw agent --json
# falls back to embedded mode (which doesn't fully load SOUL.md / IDENTITY.md)
# because scope upgrades require interactive operator approval — a
# chicken-and-egg in headless containers where the agent IS the operator.
openclaw gateway run \
  --port "${OPENCLAW_GATEWAY_PORT}" \
  --bind loopback \
  --auth none \
  --allow-unconfigured \
  --force &
GATEWAY_PID=$!

# Wait for gateway to be healthy (60s deadline).
echo "[arete] waiting for gateway health"
for i in $(seq 1 60); do
  if openclaw gateway health > /dev/null 2>&1; then
    echo "[arete] gateway healthy after ${i}s"
    break
  fi
  sleep 1
done

# Auto-grant full scopes to the loopback device. The gateway auto-pairs a
# CLI device with role=operator + scopes=[operator.read]. Agent calls
# need operator.read + operator.write + operator.pairing. The "official"
# flow requires another operator to approve scope upgrade requests; in a
# headless single-tenant container we ARE the only operator, so we hot-
# patch paired.json directly. Idempotent — re-runs on every container
# start in case the file is regenerated.
PAIRED_FILE="${STATE_HOME}/.openclaw/devices/paired.json"
# Wait briefly for paired.json to appear (gateway creates it after first
# CLI connection). Trigger a connection ourselves if needed.
for i in $(seq 1 15); do
  if [ -f "$PAIRED_FILE" ]; then break; fi
  # Trigger pairing by calling a benign CLI command
  openclaw status > /dev/null 2>&1 || true
  sleep 1
done

if [ -f "$PAIRED_FILE" ]; then
  python3 - "$PAIRED_FILE" <<'PYEOF'
import json, sys
path = sys.argv[1]
ALL = ["operator.read", "operator.write", "operator.pairing"]
try:
    with open(path) as f: data = json.load(f)
except Exception as e:
    print(f"[arete] paired.json read error: {e}", file=sys.stderr); sys.exit(0)
patched = 0
for did, dev in data.items():
    if set(dev.get("scopes", [])) >= set(ALL): continue
    dev["scopes"] = ALL
    dev["approvedScopes"] = ALL
    for role, tok in dev.get("tokens", {}).items():
        tok["scopes"] = ALL
    patched += 1
if patched:
    with open(path, "w") as f: json.dump(data, f, indent=2)
print(f"[arete] device scopes patched: {patched}/{len(data)}")
PYEOF
  # Restart gateway so it reloads paired.json. Kill the background one
  # and start a new one in the foreground via exec.
  kill -TERM "$GATEWAY_PID" 2>/dev/null || true
  wait "$GATEWAY_PID" 2>/dev/null || true
  echo "[arete] restarting gateway to reload patched scopes"
else
  echo "[arete] paired.json never appeared — gateway may need manual pairing"
  kill -TERM "$GATEWAY_PID" 2>/dev/null || true
  wait "$GATEWAY_PID" 2>/dev/null || true
fi

# Re-exec gateway in foreground (PID 1 in container).
exec openclaw gateway run \
  --port "${OPENCLAW_GATEWAY_PORT}" \
  --bind loopback \
  --auth none \
  --allow-unconfigured \
  --force
