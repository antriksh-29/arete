-- Arete state-repo pool — pre-warmed GitHub repos for sub-60s onboarding.
--
-- Q-I from architecture-research-2026-05-27.md.
--
-- Bottleneck during onboarding: `POST /repos/{template}/generate` takes
-- 5-8s. With a pool of pre-created blank repos, provisioning becomes a
-- ~1s `PATCH /repos/.../rename` instead.
--
-- Flow:
--   1. Operator runs infra/scripts/refill-repo-pool.sh N    (creates N pool repos)
--   2. Pool repos sit in state='free' until claimed
--   3. Provisioner SELECTs + claims one atomically
--   4. Provisioner renames pool repo to arete-<tenant-id>-assistant
--   5. Updates state='used', records claimed_by_tenant_id
--   6. If claim/rename fails, mark as 'broken' so refill skips it
--
-- The pool repos use a stable name pattern: arete-pool-<random-suffix>
-- (e.g. arete-pool-a3f8b1c). When claimed, GitHub rename atomically
-- moves it to the tenant-specific name.

create type repo_pool_state as enum ('free', 'used', 'broken');

create table if not exists state_repo_pool (
  name                    text primary key,             -- e.g. 'arete-pool-a3f8b1c'
  owner                   text not null,                -- 'antriksh-29'
  state                   repo_pool_state not null default 'free',
  created_at              timestamptz not null default now(),
  claimed_at              timestamptz,
  claimed_by_tenant_id    uuid references tenants(id),
  renamed_to              text,                         -- post-claim final name
  notes                   text
);

create index if not exists state_repo_pool_state_idx on state_repo_pool(state)
  where state = 'free';
create index if not exists state_repo_pool_claimed_by_idx on state_repo_pool(claimed_by_tenant_id);

-- Atomic claim helper (called from the router/provisioner).
-- Returns the name of a claimed repo, or NULL if pool is empty.
-- Caller is responsible for completing the GitHub rename and calling
-- mark_repo_used / mark_repo_broken accordingly.
create or replace function claim_repo_from_pool(p_tenant_id uuid default null)
returns text
language plpgsql as $$
declare
  v_name text;
begin
  select name into v_name
    from state_repo_pool
    where state = 'free'
    order by created_at
    for update skip locked
    limit 1;
  if v_name is null then
    return null;
  end if;
  -- Claim. tenant_id may be null if the caller doesn't have it yet — they
  -- update via mark_repo_claimed once the tenants row is inserted.
  update state_repo_pool
    set state = 'used',
        claimed_at = now(),
        claimed_by_tenant_id = p_tenant_id
    where name = v_name;
  return v_name;
end;
$$;

-- Backfill the tenant_id once the tenants row is inserted.
create or replace function mark_repo_claimed(p_pool_name text, p_tenant_id uuid)
returns void
language sql as $$
  update state_repo_pool
    set claimed_by_tenant_id = p_tenant_id
    where name = p_pool_name and state = 'used';
$$;

insert into schema_migrations(version) values ('0005_state_repo_pool')
  on conflict (version) do nothing;
