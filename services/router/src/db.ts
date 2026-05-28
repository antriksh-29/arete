import pg from "pg";
import { config } from "./config.js";
import { log } from "./log.js";

export type TenantStatus =
  | "provisioning"
  | "active"
  | "hibernated"
  | "failed"
  | "suspended";

export type OnboardingStage =
  | "awaiting_name"
  | "greeted"
  | "awaiting_country"
  | "awaiting_occupation"
  | "awaiting_use_case"
  | "complete";

export interface Tenant {
  id: string;
  telegram_user_id: number;
  telegram_username: string | null;
  name: string | null;
  country: string | null;
  occupation: string | null;
  use_case: string | null;
  timezone: string;
  state_repo_url: string;
  volume_path: string;
  image_version: string;
  internal_port: number | null;
  container_name: string | null;
  status: TenantStatus;
  onboarding_stage: OnboardingStage;
  last_active_at: Date;
  last_error: string | null;
  hibernated_at: Date | null;
  created_at: Date;
  updated_at: Date;
}

const pool = new pg.Pool({
  connectionString: config.DATABASE_URL,
  max: 10,
  idleTimeoutMillis: 30_000,
});

pool.on("error", (err) => {
  log.error({ err }, "pg pool error");
});

export async function findTenantByTelegramId(
  telegramUserId: number,
): Promise<Tenant | null> {
  const { rows } = await pool.query<Tenant>(
    `select * from tenants where telegram_user_id = $1 limit 1`,
    [telegramUserId],
  );
  return rows[0] ?? null;
}

export async function touchLastActive(tenantId: string): Promise<void> {
  await pool.query(`update tenants set last_active_at = now() where id = $1`, [
    tenantId,
  ]);
}

export async function markStatus(
  tenantId: string,
  status: TenantStatus,
  lastError?: string | null,
): Promise<void> {
  if (status === "hibernated") {
    await pool.query(
      `update tenants set status = $2, hibernated_at = now(), last_error = $3 where id = $1`,
      [tenantId, status, lastError ?? null],
    );
  } else {
    await pool.query(
      `update tenants set status = $2, last_error = $3 where id = $1`,
      [tenantId, status, lastError ?? null],
    );
  }
}

export async function findIdleActiveTenants(
  thresholdMinutes: number,
): Promise<Tenant[]> {
  const { rows } = await pool.query<Tenant>(
    `select * from tenants
       where status = 'active'
         and last_active_at < (now() - ($1 || ' minutes')::interval)`,
    [thresholdMinutes],
  );
  return rows;
}

export async function listAllTenants(): Promise<Tenant[]> {
  const { rows } = await pool.query<Tenant>(
    `select * from tenants order by created_at desc`,
  );
  return rows;
}

export async function setOnboarding(
  tenantId: string,
  stage: OnboardingStage,
  fields: {
    name?: string;
    country?: string;
    occupation?: string;
    use_case?: string;
    timezone?: string;
  } = {},
): Promise<void> {
  // Build the SET clause dynamically so we only touch the columns provided.
  const sets: string[] = ["onboarding_stage = $2"];
  const params: (string | number)[] = [tenantId, stage];
  if (fields.name !== undefined) {
    params.push(fields.name);
    sets.push(`name = $${params.length}`);
  }
  if (fields.country !== undefined) {
    params.push(fields.country);
    sets.push(`country = $${params.length}`);
  }
  if (fields.occupation !== undefined) {
    params.push(fields.occupation);
    sets.push(`occupation = $${params.length}`);
  }
  if (fields.use_case !== undefined) {
    params.push(fields.use_case);
    sets.push(`use_case = $${params.length}`);
  }
  if (fields.timezone !== undefined) {
    params.push(fields.timezone);
    sets.push(`timezone = $${params.length}`);
  }
  await pool.query(
    `update tenants set ${sets.join(", ")} where id = $1`,
    params,
  );
}

export interface UsageEventInput {
  tenant_id: string;
  source?: string;
  provider?: string | null;
  model?: string | null;
  duration_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
  ok?: boolean;
  session_id?: string | null;
  raw?: unknown;
}

/**
 * Record a usage event (per-agent-turn). Best-effort: failures are
 * logged but never thrown — we don't want telemetry write failures
 * to crash the webhook handler.
 */
export async function recordUsage(input: UsageEventInput): Promise<void> {
  try {
    await pool.query(
      `insert into usage_events (
         tenant_id, source, provider, model, duration_ms,
         input_tokens, output_tokens, cached_tokens, ok, session_id, raw
       ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
      [
        input.tenant_id,
        input.source ?? "agent_turn",
        input.provider ?? null,
        input.model ?? null,
        input.duration_ms ?? null,
        input.input_tokens ?? null,
        input.output_tokens ?? null,
        input.cached_tokens ?? null,
        input.ok ?? true,
        input.session_id ?? null,
        input.raw ? JSON.stringify(input.raw) : null,
      ],
    );
  } catch (err) {
    log.warn({ err, tenant_id: input.tenant_id }, "usage event write failed");
  }
}

export async function closeDb(): Promise<void> {
  await pool.end();
}
