# Stress-test runbook: adding a new user to Arete

Two playbooks below: (1) **smoke test** — operator-only, no real Telegram
user, runs in ~30 seconds; (2) **real new-beta-user** — give a friend the
bot link and watch end-to-end.

Run the smoke test **before** every new beta-user invite to catch
provisioning regressions cheap.

---

## 1. Smoke test (~30s, fully automated)

On `arete-1`:

```bash
cd ~/arete && sudo bash infra/scripts/smoke-new-user.sh
```

What it does:

1. Picks a synthetic `telegram_user_id` in 999_000_000–999_999_999.
2. Runs `provision-test-tenant.sh` (the same script the router invokes
   from `services/router/src/provision.ts`).
3. Asserts the new tenant row has `name=NULL`, `country=NULL`,
   `onboarding_stage='awaiting_name'`, `internal_key=<64 hex chars>`,
   `container_name=arete-<short>`.
4. Inspects the docker container env and asserts `TENANT_ID`,
   `INTERNAL_KEY`, `ROUTER_INTERNAL_URL`, `BRAIN_PATH` are all set.
5. Asserts the per-tenant volume exists at `/var/tenants/<short>/`
   with `meta.json` + `identity/USER.md` rendered.
6. POSTs a reminder to `/internal/reminders/schedule` with the new
   tenant's `internal_key` to confirm the auth path works.
7. Cleans up everything (use `--keep` to preserve state for manual
   inspection).

`SMOKE PASS` on stdout means the provisioning pipeline is healthy and
you can confidently invite a real user.

**What the smoke test does NOT cover** (real-user-only):
- The Telegram inbound webhook → router → onboarding FSM transcript
- Actual delivery of greetings / inputs/outputs explainers to a chat
- The agent-side flow: user types "remind me in 1 min" → OpenClaw →
  `skills/reminders` → `arete-remind` CLI → reminder fires
- gbrain-via-MCP queries (smoke test doesn't start the container)

For those, run playbook (2) below.

---

## 2. Real new beta user (manual, ~5 min)

### Preconditions

- You have the user's Telegram handle (e.g. `@aliceexample`).
- You confirmed they're on the beta whitelist (today: open beta, but
  document this for the day there's a gate).
- The smoke test passed in the last 24h.

### Step 1 — pre-warm the pool

Confirm at least 5 repos available in the state-repo pool:

```bash
ssh arete-1 'export DATABASE_URL=$(sudo grep ^DATABASE_URL= /etc/arete-secrets/router.env | cut -d= -f2-) && \
  psql "$DATABASE_URL" -c "select count(*) from state_repo_pool where claimed_by_tenant_id is null"'
```

If `< 5`, run the refill script first (see `arete-v2-backlog-openclaw-scopes.md`
memory for the working `/tmp/refill-pool.py` on the box).

### Step 2 — send the user the bot deep link

Give them: `https://t.me/<bot_username>?start=`

(For this beta the deep link is the bot's profile link — Telegram's `?start=`
mechanism opens with a pre-filled `/start` they just tap to send.)

### Step 3 — watch the logs

In one terminal:

```bash
ssh arete-1 'sudo journalctl -u arete-router -f' | grep -E "unknown user|provisioning|onboarding|reminder"
```

Expected sequence within ~30–60 seconds:

```
unknown user — auto-provisioning
[provision] tenant_id=<short>
[provision] provisioned. tenant_id=<short>
starting container
... onboarding FSM advances through the 4 questions ...
```

### Step 4 — verify the new tenant landed

```bash
ssh arete-1 'export DATABASE_URL=$(sudo grep ^DATABASE_URL= /etc/arete-secrets/router.env | cut -d= -f2-) && \
  psql "$DATABASE_URL" -x -c "select id, telegram_user_id, name, country, occupation, use_case, onboarding_stage, created_at from tenants order by created_at desc limit 1"'
```

After they complete the 4 onboarding questions, `onboarding_stage` should be
`complete` and all four fields populated.

### Step 5 — verify reminders work for them

Ask them to send: **"remind me in 1 minute to ping antriksh"**.

Watch the journal for `reminder fired` from their tenant_id within 60–90s.

```bash
ssh arete-1 'export DATABASE_URL=$(sudo grep ^DATABASE_URL= /etc/arete-secrets/router.env | cut -d= -f2-) && \
  psql "$DATABASE_URL" -x -c "select id, status, message, scheduled_for_utc, fired_at, last_error from reminders order by created_at desc limit 1"'
```

`status=fired`, `last_error` empty, `fired_at` within ~30s of
`scheduled_for_utc`.

### Step 6 — verify brain captures something

Ask them to tell the bot a fact: **"remember that my dog's name is Rex"**.
Wait ~30s. Then ask: **"what's my dog's name?"**

Expected: bot answers "Rex". If not: see the "Brain troubleshooting"
section below.

### Step 7 — SLA timings

| Metric | Target P50 | Target P95 |
|---|---|---|
| `/start` → first FSM message | ≤ 5s | ≤ 15s |
| Full 4-q onboarding done | ≤ 60s | ≤ 120s |
| Reminder set → delivered | scheduled_for + 0–30s | scheduled_for + 60s |
| Brain write → readable | ≤ 30s | ≤ 60s |

If any P95 misses, file a ticket and triage before the next invite.

---

## Known limitations (beta-acceptable, follow-up planned)

1. **Brain Postgres scoping**: today gbrain shares the public schema across
   tenants. Filesystem brain (markdown in `/var/tenants/<id>/brain/`) IS
   isolated; the pgvector embedding index is NOT. For 10 hand-picked beta
   users this is acceptable; per-tenant Postgres schema isolation is the
   follow-up PR (Z.2 hardening).
2. **Existing container env**: tenants provisioned BEFORE PR #63 (this
   release) need their containers recreated to get `INTERNAL_KEY` +
   `ROUTER_INTERNAL_URL` baked in. The provisioner script recreates them.
3. **Telegram from-bot-to-fake-id**: the smoke script cannot fully exercise
   the Telegram delivery path because Telegram rejects sendMessage to a
   chat_id with no prior bot interaction. The runbook above (playbook 2)
   covers the gap.
4. **Smoke test doesn't start the container**: container cold-start would
   add ~10–20s to the smoke test and isn't necessary for provisioning
   regression detection. Booting the container is gated on the first real
   inbound message (matches production behavior).

---

## Brain troubleshooting

If "remember X" → "what was X" round-trip fails for a new user:

```bash
# 1. Check the brain dir exists in the container
ssh arete-1 'sudo docker exec arete-<short> ls -la /home/agent/state/brain/'

# 2. Check the gbrain-init sentinel
ssh arete-1 'sudo docker exec arete-<short> ls /home/agent/state/brain/.gbrain-initialized'

# 3. Inspect OpenClaw config — mcp.servers.gbrain must be set
ssh arete-1 'sudo docker exec arete-<short> jq .mcp.servers.gbrain /home/agent/state/.openclaw/openclaw.json'

# 4. Run a brain query manually
ssh arete-1 'sudo docker exec arete-<short> bash -lc "BRAIN_PATH=/home/agent/state/brain gbrain query \"dog name\""'
```

If step 3 returns `null`, the `openclaw config patch` block in
`infra/entrypoint.sh` didn't apply — investigate the patch output in
`docker logs arete-<short>`.

---

## After every new user

Record in `~/.brain/people/<them>.md` (your brain, not theirs):

```yaml
provisioned_at: <ISO>
telegram_user_id: <int>
tenant_id: <uuid>
initial_use_case: <they-picked>
notes: |
  ...
```

This is your operator brain — gives you context for their weekly 1:1s
during the beta soak.
