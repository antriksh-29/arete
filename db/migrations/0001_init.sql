-- Arete initial schema (Phase C).
--
-- Multi-tenant from day one. Every domain table has tenant_id; the router
-- looks up tenants by telegram_user_id and uses the resulting tenant_id for
-- all subsequent scoping. Row-level security policies land in 0002.
--
-- Run via: db/migrate.sh

-- Extensions ---------------------------------------------------------------
create extension if not exists "uuid-ossp";
create extension if not exists vector;       -- pgvector (for brain embeddings, Phase E)

-- Schema migrations bookkeeping -------------------------------------------
create table if not exists schema_migrations (
  version     text primary key,
  applied_at  timestamptz not null default now()
);

-- Tenants ------------------------------------------------------------------
create type tenant_status as enum (
  'provisioning',  -- provisioner is creating the GitHub repo + volume
  'active',        -- container can be started; messages get routed
  'hibernated',    -- container stopped, will wake on next message
  'failed',        -- provisioning errored; needs operator attention
  'suspended'      -- billing/abuse pause; messages get rejected
);

create table if not exists tenants (
  id                  uuid primary key default uuid_generate_v4(),

  -- Identity (set at provisioning) -------------------------------------
  telegram_user_id    bigint not null unique,
  telegram_username   text,
  name                text,
  country             text,
  timezone            text default 'Etc/UTC',

  -- State storage (set at provisioning) --------------------------------
  state_repo_url      text not null,         -- e.g. https://github.com/antriksh-29/arete-<id>-assistant
  volume_path         text not null,         -- e.g. /var/tenants/<id>

  -- Runtime (set by router) --------------------------------------------
  image_version       text not null,         -- e.g. ghcr.io/antriksh-29/arete:abc123
  internal_port       int unique,            -- host port the container publishes on 127.0.0.1
  container_name      text,                  -- arete-<short_id>

  -- Lifecycle ----------------------------------------------------------
  status              tenant_status not null default 'provisioning',
  last_active_at      timestamptz not null default now(),
  last_error          text,
  hibernated_at       timestamptz,           -- when status flipped to 'hibernated'

  -- Audit --------------------------------------------------------------
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists tenants_status_idx on tenants(status);
create index if not exists tenants_last_active_active_idx
  on tenants(last_active_at)
  where status = 'active';

-- Trigger: keep updated_at fresh
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists tenants_set_updated_at on tenants;
create trigger tenants_set_updated_at
  before update on tenants
  for each row execute function set_updated_at();

-- Mark migration applied ---------------------------------------------------
insert into schema_migrations(version) values ('0001_init')
on conflict (version) do nothing;
