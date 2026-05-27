# Arete base container image
# Built from the platform repo (antriksh-29/arete) and shipped per-release.
# One container = one tenant. The per-tenant volume is bind-mounted at /home/agent/state.

FROM node:24-bookworm-slim AS base

# System deps: git (syncer + brain), python3 + uv (skill scripts), curl, ca-certs,
# build tools (some npm deps need them), playwright system deps for browser-harness.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates python3 python3-pip python3-venv \
      build-essential libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libcups2 libatspi2.0-0 \
      jq tini unzip \
    && rm -rf /var/lib/apt/lists/*

# Bun — copied from the official image (avoids curl|bash, gives us a known-good build).
COPY --from=oven/bun:1.1-debian /usr/local/bin/bun /usr/local/bin/bun

# uv (fast Python deps)
RUN curl -fsSL https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv

# Boot-gate runtime deps (eval/run-injection-fixture.py imports yaml; httpx is the
# LLM client). pip --break-system-packages required because Debian's PEP 668 marker
# otherwise blocks site-packages writes.
RUN pip3 install --break-system-packages --no-cache-dir pyyaml httpx

# Trust bind-mounted git repos regardless of UID ownership. The per-tenant volume
# is created on the host (root/UID 1000) then bind-mounted where agent is UID 1001;
# without this, every git operation fails "fatal: detected dubious ownership".
# --system writes /etc/gitconfig so it applies to all users (root + agent).
RUN git config --system --add safe.directory '*'

# OpenClaw runtime (public npm package — unscoped)
ARG OPENCLAW_VERSION=2026.5.22
RUN npm install -g openclaw@${OPENCLAW_VERSION}

# GBrain (pinned via tools/.gbrain-pin in the platform repo)
ARG GBRAIN_SHA=0fb0c83
RUN bun add -g github:garrytan/gbrain#${GBRAIN_SHA}

# Non-root agent user
RUN useradd --create-home --shell /bin/bash --uid 1001 agent \
    && mkdir -p /home/agent/state \
    && chown -R agent:agent /home/agent

# --- Platform code layer (the "base image" the user can never modify) ---
WORKDIR /opt/arete
COPY --chown=agent:agent identity/ ./identity/
COPY --chown=agent:agent skills/ ./skills/
COPY --chown=agent:agent eval/ ./eval/
COPY --chown=agent:agent infra/ ./infra/
COPY --chown=agent:agent tools/ ./tools/
COPY --chown=agent:agent meta/ ./meta/
COPY --chown=agent:agent openclaw/ ./openclaw/

# Per-skill / per-eval Python deps (skills + eval runners declare in requirements.txt)
RUN find /opt/arete/skills /opt/arete/infra /opt/arete/eval -name 'requirements.txt' -exec uv pip install --system -r {} \; 2>/dev/null || true

USER agent
WORKDIR /home/agent

# Env contract
ENV STATE_HOME=/home/agent/state \
    BRAIN_PATH=/home/agent/state/brain \
    PLATFORM_HOME=/opt/arete \
    OPENCLAW_WORKSPACE=/home/agent/state \
    PATH="/opt/arete/tools:/home/agent/.local/bin:${PATH}"

# Required env (provided by the router at `docker run`):
# - TENANT_ID
# - TENANT_REPO_URL (the per-tenant GitHub repo to clone)
# - GITHUB_PAT (for clone + push)
# - SHARED_ANTHROPIC_KEY, SHARED_OPENAI_KEY (platform LLM keys)
# - SUPABASE_DB_URL (shared, but every query scoped by tenant_id)
# - TELEGRAM_INTERNAL_PORT (router proxies inbound to this port)

# Container healthcheck — router uses this to know when a cold-start is ready
HEALTHCHECK --interval=10s --timeout=3s --retries=3 --start-period=15s \
  CMD curl -sf http://localhost:${TELEGRAM_INTERNAL_PORT:-8787}/health || exit 1

# tini for proper signal forwarding (graceful shutdown / hibernation)
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/arete/infra/entrypoint.sh"]
