# Router local test harness

Lightweight, no-CI-required tests that validate router behavior against a
real Docker daemon. Designed so we can catch dockerode quirks, exec
plumbing bugs, and container-lifecycle issues **locally** before pushing
PRs that need a full VPS deploy + Telegram round-trip to validate.

## Prerequisites

- macOS or Linux with a working Docker daemon
- Node 20+ (`brew install node` or use the project's existing Node)
- On Mac: install Colima for a lightweight Docker VM
  ```bash
  brew install colima docker
  colima start --cpu 2 --memory 3 --disk 20
  ```
  Colima exposes the docker socket at `~/.colima/default/docker.sock`.
  The tests auto-detect this; override with `DOCKER_SOCKET=...` if needed.

## Setup

```bash
cd services/router
npm install                       # installs dockerode from package.json
```

## Tests

### `dockerode-stream.test.mjs`
Reproduces the dockerode "stream.on('end') never fires when AttachStdout=false"
hang that was the root cause of PR #23. Runs the broken pattern and the fixed
pattern back-to-back against a throwaway alpine container; asserts the broken
one hangs (10s timeout) and the fixed one completes in ~2s.

```bash
node services/router/test/dockerode-stream.test.mjs
```

Expected output:
```
=== BUGGY VERSION ===
result: TIMEOUT after 10s

=== FIXED VERSION ===
result: end fired (~2s)

=== VERDICT ===
✓ Bug reproduced. ✓ PR #23 fix verified.
```

Exit code 0 = both behaviors as expected.

## Adding new tests

Mirror `dockerode-stream.test.mjs`: pull/create a throwaway container,
exercise the router code path (or a minimal copy of it) against the
container, assert on outcome + timing, clean up.

Tests must:
1. Be runnable without any GHCR / Anthropic / Supabase credentials
   (use throwaway alpine containers, not the full arete image)
2. Complete in <60 seconds
3. Clean up after themselves (stop + remove containers)
4. Exit non-zero on any failure
