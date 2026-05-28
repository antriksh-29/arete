-- Arete usage tracking — per-agent-turn telemetry for cost attribution.
--
-- Q-Q from architecture-research-2026-05-27.md. Approach: router parses
-- the JSON envelope returned by `openclaw agent --json` and writes a row
-- per turn. Model + duration come from `result.meta.agentMeta`. Token
-- counts come from `result.meta.tokens` if openclaw populates them
-- (varies by model + version); fall back to NULL when not available.
--
-- Aggregations (month-to-date cost per tenant, model mix, etc.) are
-- computed by views or nightly jobs, not by inline triggers — keep the
-- write path fast.

create table if not exists usage_events (
  id                bigserial primary key,
  tenant_id         uuid not null references tenants(id) on delete cascade,
  ts                timestamptz not null default now(),
  source            text not null default 'agent_turn',  -- 'agent_turn'|'cron'|'background'
  provider          text,        -- 'anthropic' | 'deepseek' | 'moonshot' | 'openai' | ...
  model             text,        -- 'deepseek/deepseek-v4-pro', 'anthropic/claude-haiku-4-5', ...
  duration_ms       integer,
  input_tokens      integer,
  output_tokens     integer,
  cached_tokens     integer,
  ok                boolean not null default true,       -- false when openclaw exit != 0
  session_id        text,
  raw               jsonb         -- full agent meta for forensic queries
);

create index if not exists usage_events_tenant_ts_idx on usage_events(tenant_id, ts desc);
create index if not exists usage_events_model_ts_idx on usage_events(model, ts desc);
create index if not exists usage_events_ts_idx on usage_events(ts desc);

-- Convenience view: month-to-date row count + duration per tenant.
-- Costs are computed in application code (deepseek pricing is permanent
-- $0.435/$0.87, anthropic-haiku is $1/$5, etc. — see docs/architecture-research)
-- to avoid baking model rate cards into Postgres.
create or replace view usage_mtd as
select
  tenant_id,
  count(*) as turn_count,
  sum(duration_ms) as total_duration_ms,
  sum(coalesce(input_tokens, 0))  as total_input_tokens,
  sum(coalesce(output_tokens, 0)) as total_output_tokens,
  sum(coalesce(cached_tokens, 0)) as total_cached_tokens,
  count(*) filter (where not ok) as failed_turn_count
from usage_events
where ts >= date_trunc('month', now())
group by tenant_id;

insert into schema_migrations(version) values ('0004_usage_events')
  on conflict (version) do nothing;
