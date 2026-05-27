#!/usr/bin/env bash
# deepseek-proxy-launch.sh — read API key from arete-cred vault and exec
# the Node proxy. Used by infra/deepseek-proxy.service as the ExecStart
# command. Mirrors infra/moonshot-proxy-launch.sh.

set -uo pipefail

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

BOTCRED=""
for p in "$HOME/.local/bin/arete-cred" "/usr/local/bin/arete-cred" "$(command -v arete-cred 2>/dev/null)"; do
  if [ -x "$p" ]; then BOTCRED="$p"; break; fi
done

if [ -z "$BOTCRED" ]; then
  echo "[deepseek-proxy-launch] FATAL: arete-cred not found on PATH" >&2
  exit 1
fi

export DEEPSEEK_API_KEY="$("$BOTCRED" get DEEPSEEK_API_KEY 2>/dev/null | head -1)"
export PROXY_PORT="${PROXY_PORT:-18791}"
export FALLBACK_PROXY_PORT="${FALLBACK_PROXY_PORT:-18790}"
export FALLBACK_MODEL="${FALLBACK_MODEL:-kimi-k2.6}"
# Thinking-mode experiment knob. Default disabled (matches Kimi instant
# discipline that prevents 180s gateway timeouts). Flip to "enabled" via
# `systemctl --user edit deepseek-proxy.service` and add:
#   [Service]
#   Environment=DEEPSEEK_THINKING=enabled
#   Environment=DEEPSEEK_REASONING_EFFORT=medium
# then `systemctl --user restart deepseek-proxy.service`.
export DEEPSEEK_THINKING="${DEEPSEEK_THINKING:-disabled}"
export DEEPSEEK_REASONING_EFFORT="${DEEPSEEK_REASONING_EFFORT:-}"

NODE_BIN=""
for p in "$HOME/.bun/bin/node" "$HOME/.local/share/fnm/current/bin/node" "$HOME/.nvm/current/bin/node" "$HOME/.volta/bin/node" "/usr/local/bin/node" "/usr/bin/node"; do
  if [ -x "$p" ]; then NODE_BIN="$p"; break; fi
done
if [ -z "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node 2>/dev/null || true)"
fi
if [ -z "$NODE_BIN" ]; then
  echo "[deepseek-proxy-launch] FATAL: node not found on PATH" >&2
  exit 1
fi

exec "$NODE_BIN" "$HOME/personal-assistant/infra/deepseek-proxy.mjs"
