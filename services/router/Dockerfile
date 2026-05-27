# Build image for the Arete router service.
# Built and run separately from the tenant image (different lifecycle, different deps).

FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY package.json tsconfig.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY src ./src
RUN npm run build

FROM node:24-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      tini ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY package.json ./
RUN --mount=type=cache,target=/root/.npm npm install --omit=dev
COPY --from=build /app/dist ./dist

# The router needs to talk to dockerd via /var/run/docker.sock — mounted at runtime
# via -v /var/run/docker.sock:/var/run/docker.sock. We do NOT run as root; we add
# the agent user to the docker group at runtime by mounting in a matching gid.
RUN useradd --create-home --shell /bin/bash --uid 1001 agent
USER agent

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=3 --start-period=5s \
  CMD curl -sf http://localhost:8080/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "node", "dist/index.js"]
