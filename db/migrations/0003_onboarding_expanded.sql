-- Arete extended onboarding (Phase D v2).
--
-- Per direct product feedback: the first-time conversation should also
-- capture *what the user does* and *what they want to use Arete for*, so
-- the bot can suggest a starter journey (set up reminders, triage emails,
-- daily briefings, etc.) instead of dumping a generic list of capabilities.
--
-- Extends the FSM from 0002_onboarding.sql with two more capture states
-- and a "showing_capabilities" pause before the user's first agent turn:
--
--   awaiting_name → greeted → awaiting_country → awaiting_occupation
--   → awaiting_use_case → complete
--
-- (We drop the separate "showing_capabilities" pre-complete state because
-- the capabilities reveal is just the welcome message tied to the
-- awaiting_use_case → complete transition. No extra state needed.)

-- Add new states to the existing enum
alter type onboarding_stage add value if not exists 'awaiting_occupation';
alter type onboarding_stage add value if not exists 'awaiting_use_case';

-- New profile columns. Both nullable; populated during onboarding turns 4-5.
alter table tenants
  add column if not exists occupation text,
  add column if not exists use_case   text;

create index if not exists tenants_use_case_idx on tenants(use_case)
  where use_case is not null;

insert into schema_migrations(version) values ('0003_onboarding_expanded')
  on conflict (version) do nothing;
