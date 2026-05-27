import type { Tenant } from "./db.js";
import { log } from "./log.js";

/**
 * Forward a Telegram webhook payload to the tenant container's internal port.
 * Returns the container's response body (which we relay back to Telegram).
 */
export async function proxyWebhook(
  tenant: Tenant,
  body: unknown,
): Promise<{ status: number; body: string }> {
  if (!tenant.internal_port) {
    throw new Error(`tenant ${tenant.id} has no internal_port`);
  }
  const url = `http://127.0.0.1:${tenant.internal_port}/telegram/webhook`;

  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120_000), // long-running agent turns can take a while
  });

  const text = await res.text();
  log.debug(
    { tenant: tenant.id, status: res.status, bytes: text.length },
    "proxied webhook to tenant",
  );
  return { status: res.status, body: text };
}
