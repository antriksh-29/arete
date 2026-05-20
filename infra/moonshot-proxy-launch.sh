#!/usr/bin/env bash
# moonshot-proxy-launch.sh — read API keys from bot-cred vault and exec
# the Node proxy. Used by infra/moonshot-proxy.service as the ExecStart
# command. Lives outside the systemd-unit-managed env-file pattern so we
# don't hit the "EnvironmentFile doesn't exist on first boot" race that
# blocks the unit from starting.

set -uo pipefail

# bot-cred isn't always on systemd's PATH. Add the standard user-bin paths.
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Locate bot-cred — try a few common paths
BOTCRED=""
for p in "$HOME/.local/bin/bot-cred" "/usr/local/bin/bot-cred" "$(command -v bot-cred 2>/dev/null)"; do
  if [ -x "$p" ]; then BOTCRED="$p"; break; fi
done

if [ -z "$BOTCRED" ]; then
  echo "[moonshot-proxy-launch] FATAL: bot-cred not found on PATH" >&2
  exit 1
fi

# Read keys from vault. If a key is missing the proxy still starts but
# logs a warning — the corresponding upstream will be unavailable.
export MOONSHOT_API_KEY="$("$BOTCRED" get MOONSHOT_API_KEY 2>/dev/null | head -1)"
export OPENAI_API_KEY="$("$BOTCRED" get OPENAI_API_KEY 2>/dev/null | head -1)"
export PROXY_PORT="${PROXY_PORT:-18790}"
export FALLBACK_MODEL="${FALLBACK_MODEL:-gpt-5.4-mini}"

# Locate node — same dance as bot-cred. Node is often only on PATH via
# fnm/nvm/volta wrappers that systemd doesn't see.
NODE_BIN=""
for p in "$HOME/.bun/bin/node" "$HOME/.local/share/fnm/current/bin/node" "$HOME/.nvm/current/bin/node" "$HOME/.volta/bin/node" "/usr/local/bin/node" "/usr/bin/node"; do
  if [ -x "$p" ]; then NODE_BIN="$p"; break; fi
done
if [ -z "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node 2>/dev/null || true)"
fi
if [ -z "$NODE_BIN" ]; then
  echo "[moonshot-proxy-launch] FATAL: node not found on PATH" >&2
  exit 1
fi

# exec replaces this shell with node — systemd tracks the node PID directly
exec "$NODE_BIN" "$HOME/personal-assistant/infra/moonshot-proxy.mjs"
