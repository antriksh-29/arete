# Arete CI workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `build-image.yml` | `push` to `main`, tagged releases, PRs touching image inputs | Builds `infra/Dockerfile`, pushes to `ghcr.io/antriksh-29/arete`. PR builds verify-only (no push). |

## GHCR authentication

The workflow uses the built-in `GITHUB_TOKEN`. No additional secrets needed. The first successful push will create the package at `ghcr.io/antriksh-29/arete`. **After the first push**, go to the package page on GitHub and set the visibility to **Private** (default is Private but worth confirming).

## Image tags

- `latest` — most recent `main`
- `<short-sha>` — pinned to a specific commit (use this in prod)
- `vMAJOR.MINOR.PATCH` — released versions (created when you push a `v*` git tag)

## Local build (no CI required)

```bash
docker compose build
# or directly:
docker build -f infra/Dockerfile -t arete:local .
```

## Cache

GitHub Actions cache (`type=gha`) is used to speed up subsequent builds. The `apt-get install` layer + Bun/uv install layer are the slowest; they get cached between runs.

## Future workflows (not yet implemented)

- `release.yml` — triggers fleet rolling-update on the host when a new image tag ships (Phase G)
- `e2e.yml` — spins up a test tenant in CI and runs end-to-end webhook tests (Phase C)
