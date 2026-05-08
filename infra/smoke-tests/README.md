# Drop-in smoke tests for gbrain

These shell scripts plug into gbrain's user-extensible smoke-test hook
at `~/.gbrain/smoke-tests.d/`. Each one is a drop-in that runs `--smoke`
against a deployed skill script and exits 0 (pass) or non-zero (fail).

## How they're picked up

Per `gbrain smoke-test`, every executable file in `~/.gbrain/smoke-tests.d/`
is run in alphabetical order during `gbrain smoke-test`. Conventions:

- Filename prefix `50-` so we land between gbrain's built-in tests
  and any later user drop-ins.
- Exit 0 = pass. Non-zero = fail. Stdout is captured into the smoke log.
- Scripts must be safe to run repeatedly with no side effects.

## Deploy

```bash
bash infra/sync-smoke-tests.sh         # copies all scripts here to ~/.gbrain/smoke-tests.d/
bash infra/sync-smoke-tests.sh --check # verifies sync without copying
```

## Why these exist

Per Wave 3 (G11): every skill with a `--smoke` flag should be invoked
on cold boot, surfacing failures before the user notices them. The
infrastructure for "every skill is callable" is already shipped by
gbrain — we plug into it rather than rebuild it.

When adding a new skill: drop a new `50-skill-<name>.sh` here following
the same shape as the existing ones. The audit
(`infra/check-resolvable.mjs`) doesn't yet cross-reference these, but
that's a Wave 4 governance follow-up.
