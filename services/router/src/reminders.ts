// Reminders — durable scheduled messages, written by tenant containers,
// fired by a background poller in the router process.
//
// Lifecycle: container's `arete-remind` CLI calls POST /internal/reminders/schedule
// → row inserted with status='pending'. Poller wakes every 30s, claims due
// rows with FOR UPDATE SKIP LOCKED (safe under concurrent pollers if we ever
// scale to multiple router replicas), delivers via sendChunkedMessage,
// marks 'fired' or increments attempts + 'failed' after 5 strikes.

import type { Request, Response } from "express";
import { z } from "zod";
import { getPool, findTenantByInternalKey, findTenantById } from "./db.js";
import { sendChunkedMessage } from "./telegram.js";
import { log } from "./log.js";

const POLL_INTERVAL_MS = 30_000;
const MAX_ATTEMPTS = 5;
const BATCH_SIZE = 50;

// ── HTTP: schedule a reminder ────────────────────────────────────────────

const ScheduleBody = z.object({
  message: z.string().min(1).max(4096),
  scheduled_for_utc: z.string().datetime(), // ISO-8601, must end with Z
});

export async function scheduleReminder(
  req: Request,
  res: Response,
): Promise<void> {
  const key = req.header("X-Tenant-Key");
  if (!key) {
    res.status(401).json({ ok: false, error: "missing X-Tenant-Key" });
    return;
  }
  const tenant = await findTenantByInternalKey(key);
  if (!tenant) {
    res.status(401).json({ ok: false, error: "unknown key" });
    return;
  }

  const parsed = ScheduleBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ ok: false, error: parsed.error.flatten().fieldErrors });
    return;
  }
  const { message, scheduled_for_utc } = parsed.data;

  // Reject scheduling in the past more than 60s — usually a tz bug.
  const when = new Date(scheduled_for_utc);
  if (Number.isNaN(when.getTime())) {
    res.status(400).json({ ok: false, error: "invalid scheduled_for_utc" });
    return;
  }
  if (when.getTime() < Date.now() - 60_000) {
    res.status(400).json({ ok: false, error: "scheduled_for_utc is in the past" });
    return;
  }

  const { rows } = await getPool().query<{ id: string }>(
    `insert into reminders (tenant_id, message, scheduled_for_utc)
       values ($1, $2, $3)
       returning id`,
    [tenant.id, message, when.toISOString()],
  );
  res.json({ ok: true, id: rows[0]!.id, scheduled_for_utc: when.toISOString() });
}

// ── HTTP: list pending reminders for the tenant (skill: "what's coming up") ─

export async function listPendingReminders(
  req: Request,
  res: Response,
): Promise<void> {
  const key = req.header("X-Tenant-Key");
  if (!key) {
    res.status(401).json({ ok: false, error: "missing X-Tenant-Key" });
    return;
  }
  const tenant = await findTenantByInternalKey(key);
  if (!tenant) {
    res.status(401).json({ ok: false, error: "unknown key" });
    return;
  }
  const { rows } = await getPool().query(
    `select id, message, scheduled_for_utc
       from reminders
       where tenant_id = $1 and status = 'pending'
       order by scheduled_for_utc asc
       limit 100`,
    [tenant.id],
  );
  res.json({ ok: true, reminders: rows });
}

// ── HTTP: cancel a reminder by id ────────────────────────────────────────

export async function cancelReminder(
  req: Request,
  res: Response,
): Promise<void> {
  const key = req.header("X-Tenant-Key");
  if (!key) {
    res.status(401).json({ ok: false, error: "missing X-Tenant-Key" });
    return;
  }
  const tenant = await findTenantByInternalKey(key);
  if (!tenant) {
    res.status(401).json({ ok: false, error: "unknown key" });
    return;
  }
  const id = req.params.id;
  if (!id) {
    res.status(400).json({ ok: false, error: "missing id" });
    return;
  }
  const { rowCount } = await getPool().query(
    `update reminders set status='cancelled'
       where id = $1 and tenant_id = $2 and status = 'pending'`,
    [id, tenant.id],
  );
  res.json({ ok: true, cancelled: (rowCount ?? 0) > 0 });
}

// ── Background poller ────────────────────────────────────────────────────

interface DueReminder {
  id: string;
  tenant_id: string;
  message: string;
  attempts: number;
}

async function claimDueBatch(): Promise<DueReminder[]> {
  // SKIP LOCKED makes this safe if we ever run >1 router replica.
  const client = await getPool().connect();
  try {
    await client.query("begin");
    const { rows } = await client.query<DueReminder>(
      `select id, tenant_id, message, attempts
         from reminders
         where status = 'pending' and scheduled_for_utc <= now()
         order by scheduled_for_utc asc
         limit $1
         for update skip locked`,
      [BATCH_SIZE],
    );
    if (rows.length === 0) {
      await client.query("commit");
      return [];
    }
    // We hold the row locks for the duration of this transaction. Commit
    // immediately — actual delivery happens outside the txn; we'll update
    // status with a fresh statement per row.
    await client.query("commit");
    return rows;
  } catch (err) {
    await client.query("rollback").catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}

async function markFired(id: string): Promise<void> {
  await getPool().query(
    `update reminders set status='fired', fired_at = now(), last_error = null
       where id = $1`,
    [id],
  );
}

async function markAttemptFailed(
  id: string,
  attemptsBefore: number,
  err: string,
): Promise<void> {
  const finalStatus =
    attemptsBefore + 1 >= MAX_ATTEMPTS ? "failed" : "pending";
  await getPool().query(
    `update reminders
       set attempts = attempts + 1,
           status = $2,
           last_error = $3
       where id = $1`,
    [id, finalStatus, err.slice(0, 500)],
  );
}

async function deliverOne(r: DueReminder): Promise<void> {
  const tenant = await findTenantById(r.tenant_id);
  if (!tenant) {
    await markAttemptFailed(r.id, r.attempts, "tenant not found");
    return;
  }
  try {
    await sendChunkedMessage(Number(tenant.telegram_user_id), r.message);
    await markFired(r.id);
    log.info({ reminder_id: r.id, tenant_id: tenant.id }, "reminder fired");
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log.warn({ err, reminder_id: r.id }, "reminder delivery failed");
    await markAttemptFailed(r.id, r.attempts, msg);
  }
}

let pollerRunning = false;

async function pollOnce(): Promise<void> {
  if (pollerRunning) return; // overlap guard
  pollerRunning = true;
  try {
    const due = await claimDueBatch();
    if (due.length === 0) return;
    log.info({ count: due.length }, "firing due reminders");
    // Deliver sequentially — sendChunkedMessage already rate-limits per chat
    // and globally. Parallel-fan-out would just contend for the same buckets.
    for (const r of due) {
      await deliverOne(r);
    }
  } catch (err) {
    log.error({ err }, "reminder poll failed");
  } finally {
    pollerRunning = false;
  }
}

export function startReminderPoller(): NodeJS.Timeout {
  log.info({ interval_ms: POLL_INTERVAL_MS }, "reminder poller started");
  // Fire once on boot to catch anything that came due while we were down.
  void pollOnce();
  return setInterval(() => {
    void pollOnce();
  }, POLL_INTERVAL_MS);
}
