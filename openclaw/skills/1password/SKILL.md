---
name: 1password
version: 1
script: null
mutating: true
cron: null
eval: openclaw/skills/1password/eval.yaml
last_audited: 2026-05-16
description: |
  Set up and use the 1Password CLI (`op`) to sign in, read secrets, inject
  credentials, and list vaults. Requires the 1Password desktop app for
  biometric/app integration. All `op` commands must run inside a tmux session.
  WHEN: "get my GitHub PAT from 1password", "what's the password for X",
  "inject my AWS keys from 1password", "sign in to 1password", "list my vaults".
  NOT WHEN: secrets are already in env vars; user wants to store a web password
  interactively (use the 1Password app directly instead).
trigger: |
  - "get my <secret> from 1password"
  - "what's the password for <service> in 1password"
  - "sign in to 1password"
  - "list my 1password vaults"
  - "inject secrets from 1password into <command>"
  - "op whoami"
  - "read item <name> from 1password"
  - "use 1password to fill <credential>"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/1password
---

# 1Password CLI

Follow the official CLI get-started steps. Don't guess install commands.

## References

- `references/get-started.md` (install + app integration + sign-in flow)
- `references/cli-examples.md` (real `op` examples)

## Workflow

1. Check OS + shell.
2. Verify CLI present: `op --version`.
3. Confirm desktop app integration is enabled (per get-started) and the app is unlocked.
4. REQUIRED: create a fresh tmux session for all `op` commands (no direct `op` calls outside tmux).
5. Sign in / authorize inside tmux: `op signin` (expect app prompt).
6. Verify access inside tmux: `op whoami` (must succeed before any secret read).
7. If multiple accounts: use `--account` or `OP_ACCOUNT`.

## REQUIRED tmux session (T-Max)

The shell tool uses a fresh TTY per command. To avoid re-prompts and failures, always run `op` inside a dedicated tmux session with a fresh socket/session name.

Example (see `tmux` skill for socket conventions, do not reuse old session names):

```bash
SOCKET_DIR="${OPENCLAW_TMUX_SOCKET_DIR:-${TMPDIR:-/tmp}/openclaw-tmux-sockets}"
mkdir -p "$SOCKET_DIR"
SOCKET="$SOCKET_DIR/openclaw-op.sock"
SESSION="op-auth-$(date +%Y%m%d-%H%M%S)"

tmux -S "$SOCKET" new -d -s "$SESSION" -n shell
tmux -S "$SOCKET" send-keys -t "$SESSION":0.0 -- "op signin --account my.1password.com" Enter
tmux -S "$SOCKET" send-keys -t "$SESSION":0.0 -- "op whoami" Enter
tmux -S "$SOCKET" send-keys -t "$SESSION":0.0 -- "op vault list" Enter
tmux -S "$SOCKET" capture-pane -p -J -t "$SESSION":0.0 -S -200
tmux -S "$SOCKET" kill-session -t "$SESSION"
```

## Guardrails

- Never paste secrets into logs, chat, or code.
- Prefer `op run` / `op inject` over writing secrets to disk.
- If sign-in without app integration is needed, use `op account add`.
- If a command returns "account is not signed in", re-run `op signin` inside tmux and authorize in the app.
- Do not run `op` outside tmux; stop and ask if tmux is unavailable.
