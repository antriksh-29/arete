# GitHub Actions — auto-deploy to VPS

`deploy.yml` runs on every push to `main` and SSHes into the bot VPS to
run `git pull && bash infra/deploy-vps.sh`. The pre-push hook
(`infra/install-pre-push-hook.sh`) gates the *push*; this workflow
gates the *deploy*.

## One-time setup

Add these four GitHub repository secrets under Settings → Secrets and
variables → Actions → Repository secrets:

| Secret name | Value |
|---|---|
| `VPS_HOST` | `{{platform.domain}}` |
| `VPS_USER` | `agent` |
| `VPS_SSH_KEY` | A *new* dedicated deploy SSH key's **private** half. **Do NOT reuse your personal key.** Generate with `ssh-keygen -t ed25519 -f ~/.ssh/id_personal_assistant_deploy -N "" -C "github-actions deploy"`. Add the **public** half (`~/.ssh/id_personal_assistant_deploy.pub`) to `~/.ssh/authorized_keys` on the VPS as `agent`. |
| `VPS_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519,rsa {{platform.domain}}` run from your Mac. Pin the host key so MITM is impossible from a hijacked GitHub runner. |

### Generating the deploy key (one-time)

On your Mac:

```bash
ssh-keygen -t ed25519 \
  -f ~/.ssh/id_personal_assistant_deploy \
  -N "" \
  -C "github-actions personal-assistant deploy"

# Show the public key so you can paste it onto the VPS:
cat ~/.ssh/id_personal_assistant_deploy.pub

# Show the private key — copy this into the VPS_SSH_KEY secret:
cat ~/.ssh/id_personal_assistant_deploy
```

On the VPS (one-time, while still SSHed in as `agent` from your Mac):

```bash
echo '<paste public key here>' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

For the known-hosts pin (run on your Mac):

```bash
ssh-keyscan -t ed25519,rsa {{platform.domain}}
# Copy the output verbatim into the VPS_KNOWN_HOSTS secret.
```

## What the workflow does

1. **Single-flight concurrency** — a new push to `main` cancels any older
   deploy still in flight, so two pushes don't race.
2. **Set up SSH** — writes the deploy key + known-hosts pin into the
   runner; configures the `bot` host alias.
3. **`ssh bot 'cd ~/personal-assistant && git fetch && git reset --hard
   origin/main && bash infra/deploy-vps.sh'`** — the real deploy.
   Hard reset (not pull) so a divergent local state on the VPS can't
   block the deploy.
4. **Post-deploy smoke probe** — confirms `openclaw-gateway.service`
   is `active` after the deploy. If it crashed, the workflow fails
   with a `::error::` annotation.

## What it does NOT do

- **No rollback.** If the deploy lands but the bot misbehaves, you SSH
  in and `git reset --hard <previous-sha>` manually.
- **No DB migrations.** If we ever add stateful migrations, that's a
  separate workflow step.
- **No Slack/Telegram notify on success.** Only failures notify (via
  GitHub's built-in workflow failure email).

## Manual deploy fallback

If GitHub Actions is down (rare) or you want to test a deploy script
change without merging it first:

```bash
ssh bot
cd ~/personal-assistant
git fetch
git checkout <branch>
bash infra/deploy-vps.sh
```

Or from your Mac with one command:

```bash
ssh bot 'cd ~/personal-assistant && git pull && bash infra/deploy-vps.sh'
```
// triggered by GHA deploy verification — 2026-05-10
