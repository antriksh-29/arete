-- Arete reminders — durable per-tenant scheduled messages.
--
-- Phase Z.1. A tenant's agent calls into the router's internal API to
-- schedule a reminder; the router writes a row here. A background poller
-- in the router process picks due rows every 30s and delivers via the
-- existing Telegram sender (chunked + rate-limited).
--
-- Why a Postgres table (and not systemd-run, which is what Alfred used):
-- containers are ephemeral. They hibernate after 30 min idle and may be
-- restarted on platform updates. A timer that lives inside a hibernated
-- container won't fire. Postgres survives both.
--
-- Authentication for container → router calls: every tenant gets an
-- internal_key (random 32-byte hex) at provisioning, injected into the
-- container's env. The router maps key → tenant_id server-side, so a
-- compromised container can only schedule reminders for itself.

create extension if not exists pgcrypto;  -- gen_random_bytes()

-- Per-tenant auth secret for container→router internal calls.
-- Backfilled at migration time for existing rows; provisioner generates
-- the key for new rows.
alter table tenants
  add column if not exists internal_key text;

update tenants
  set internal_key = encode(gen_random_bytes(32), 'hex')
  where internal_key is null;

alter table tenants
  alter column internal_key set not null,
  alter column internal_key set default encode(gen_random_bytes(32), 'hex');

create unique index if not exists tenants_internal_key_idx on tenants(internal_key);

-- Reminders ----------------------------------------------------------------
create type reminder_status as enum (
  'pending',     -- scheduled, not yet due
  'fired',       -- delivered to Telegram
  'failed',      -- exhausted retries
  'cancelled'    -- user cancelled before firing
);

create table if not exists reminders (
  id                  uuid primary key default uuid_generate_v4(),
  tenant_id           uuid not null references tenants(id) on delete cascade,

  -- What to fire ------------------------------------------------------
  message             text not null,                  -- final user-facing text
  scheduled_for_utc   timestamptz not null,           -- always UTC; tenant tz applied upstream

  -- State -------------------------------------------------------------
  status              reminder_status not null default 'pending',
  attempts            integer not null default 0,
  fired_at            timestamptz,                    -- set when status flips to 'fired'
  last_error          text,                           -- last delivery error message

  -- Audit -------------------------------------------------------------
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  -- Soft constraints
  constraint reminders_message_nonempty check (length(btrim(message)) > 0),
  constraint reminders_message_lte_4096 check (length(message) <= 4096)
);

-- Poller's hot path: due + pending rows, ordered.
create index if not exists reminders_due_idx
  on reminders(scheduled_for_utc)
  where status = 'pending';

-- Per-tenant listing (for /reminders skill: "what do I have coming up?").
create index if not exists reminders_tenant_pending_idx
  on reminders(tenant_id, scheduled_for_utc)
  where status = 'pending';

drop trigger if exists reminders_set_updated_at on reminders;
create trigger reminders_set_updated_at
  before update on reminders
  for each row execute function set_updated_at();

insert into schema_migrations(version) values ('0006_reminders')
  on conflict (version) do nothing;
