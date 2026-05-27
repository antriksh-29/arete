# tools/

Local Python tools that run alongside OpenClaw. All built in Phase 2 unless noted.

| File | Phase | Purpose |
|------|-------|---------|
| `creds.py` | 2 | Credential vault — encrypted SQLite + setuid wrapper. ~80 LOC. |
| `arete-cred` | 2 | CLI wrapper around `creds.py`: `arete-cred get NAME`, `arete-cred set NAME`, `arete-cred list`, `arete-cred audit`. |
| `safe_fs.py` | 2 | Path-enforcement module. `safe_resolve(path) → str` rejects anything outside workspace. **CRITICAL** — 20 OWASP traversal tests must pass. |
| `log_shipper.py` | 2 | Tails `/var/log/openclaw/*.log`, ships structured JSON to Axiom dataset `bot-logs`. Includes `redact()` for credential patterns. |
| `oauth_bootstrap.py` | 2 | One-time script to fetch Gmail + Calendar refresh tokens. Run on laptop, copies tokens into VPS vault via `arete-cred set`. |
| `smoke_test.py` | 2 | Runs `_smoke/injection-fixture.yaml` through the agent at boot. Returns non-zero on any miss → systemd `ExecStartPre` blocks startup. |
| `.gbrain-pin` | 0 (placeholder) → 4 | GBrain SHA pin. |

Phase 0 status: only `.gbrain-pin` exists (placeholder).
