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
  await pool.query(
    `update tenants set ${sets.join(", ")} where id = $1`,
    params,
  );
}

export async function closeDb(): Promise<void> {
  await pool.end();
}
