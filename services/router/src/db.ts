import pg from "pg";
import { config } from "./config.js";
import { log } from "./log.js";

export type TenantStatus =
  | "provisioning"
  | "active"
  | "hibernated"
  | "failed"
  | "suspended";

export interface Tenant {
  id: string;
  telegram_user_id: number;
  telegram_username: string | null;
  name: string | null;
  country: string | null;
  timezone: string;
  state_repo_url: string;
  volume_path: string;
  image_version: string;
  internal_port: number | null;
  container_name: string | null;
  status: TenantStatus;
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

export async function closeDb(): Promise<void> {
  await pool.end();
}
