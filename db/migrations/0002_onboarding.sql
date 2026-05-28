-- Arete onboarding-stage tracking (Phase D).
--
-- Adds an onboarding_stage column so the router can branch on first-time-
-- conversation logic without calling the agent. Per the 2026-05-27
-- architecture research (Q-K): turn-1 should be a router-side scripted
-- greeting that asks name + country, persists, then yields to the agent
-- from turn-3+. Saves cold-start latency + LLM cost on the first message
-- where the user hasn't yet identified themselves.
--
-- States:
--   awaiting_name     — bot will greet + ask name on next inbound. After
--                       reply, save name and transition to awaiting_country.
--   awaiting_country  — bot will ask country. After reply, save country
--                       and transition to complete.
--   complete          — route every message to the agent normally.
--
-- Existing tenants where the provisioner filled in name+country (e.g. the
-- internal test tenant) skip onboarding and start in 'complete'.

-- FSM states (in order):
--   awaiting_name     — fresh tenant. Next inbound triggers a greeting + ask
--                       for name. Transitions to 'greeted'.
--   greeted           — we've greeted + asked for name. Next inbound IS the
--                       name. Save it, ask country, transition to
--                       'awaiting_country'.
--   awaiting_country  — next inbound IS the country. Save, send welcome,
--                       transition to 'complete'.
--   complete          — route every message to the agent.
create type onboarding_stage as enum (
  'awaiting_name',
  'greeted',
  'awaiting_country',
  'complete'
);

alter table tenants
  add column if not exists onboarding_stage onboarding_stage
    not null default 'awaiting_name';

-- Backfill: anyone who already has name + country (provisioner pre-filled)
-- skips the scripted onboarding.
update tenants
   set onboarding_stage = 'complete'
 where name is not null
   and country is not null
   and onboarding_stage = 'awaiting_name';

create index if not exists tenants_onboarding_stage_idx
  on tenants(onboarding_stage)
  where onboarding_stage <> 'complete';

insert into schema_migrations(version) values ('0002_onboarding')
  on conflict (version) do nothing;
