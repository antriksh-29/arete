// Production-signup provisioning.
//
// Called by webhook.ts when an unknown telegram_user_id sends a message.
// Shells out to the bash provisioner (which already handles repo claim,
// volume clone, docker create, tenants INSERT). In production mode the
// provisioner is invoked WITHOUT --name / --country so the new tenant
// row defaults to onboarding_stage='awaiting_name'.
//
// Sudoers gate: install-router.sh writes /etc/sudoers.d/arete-router to
// allow the `arete` user (router runs as) to run `sudo bash
// /opt/arete/scripts/provision-tenant.sh ...` without a password. Only
// that exact path with controlled args is permitted.
import { spawn } from "child_process";
import { findTenantByTelegramId, type Tenant } from "./db.js";
import { log } from "./log.js";

const PROVISIONER_PATH = "/opt/arete/scripts/provision-tenant.sh";
// 5-minute hard cap. Typical: 8-15s with pool, 25-40s with template-generate.
const PROVISION_TIMEOUT_MS = 5 * 60_000;

// In-memory dedup: if two webhooks for the same telegram_user_id race
// (user sends 3 messages while we're provisioning the first), only one
// provisioning runs; the others await the same promise.
const inFlight = new Map<number, Promise<Tenant | null>>();

interface ProvisionInput {
  telegramUserId: number;
  telegramUsername: string | null;
}

async function runProvisionerScript(args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const p = spawn("sudo", ["-n", "bash", PROVISIONER_PATH, ...args], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    p.stdout.on("data", (d: Buffer) => (stdout += d.toString("utf8")));
    p.stderr.on("data", (d: Buffer) => (stderr += d.toString("utf8")));
    const killer = setTimeout(() => p.kill("SIGKILL"), PROVISION_TIMEOUT_MS);
    p.on("close", (code) => {
      clearTimeout(killer);
      resolve({ exitCode: code ?? -1, stdout, stderr });
    });
  });
}

/**
 * Provision a tenant for an unknown telegram_user_id. Idempotent: if the
 * row already exists (race), the bash script's ON CONFLICT updates it
 * safely and we just refetch. Coalesces concurrent calls for the same
 * user via inFlight map.
 */
export async function provisionNewTenant(input: ProvisionInput): Promise<Tenant | null> {
  const existing = inFlight.get(input.telegramUserId);
  if (existing) {
    log.info(
      { telegram_user_id: input.telegramUserId },
      "joining in-flight provision",
    );
    return existing;
  }

  const work = (async (): Promise<Tenant | null> => {
    const t0 = Date.now();
    log.info(
      { telegram_user_id: input.telegramUserId, username: input.telegramUsername },
      "provisioning new tenant",
    );
    const args = ["--telegram-user-id", String(input.telegramUserId)];
    if (input.telegramUsername) {
      args.push("--username", input.telegramUsername);
    }
    const r = await runProvisionerScript(args);
    if (r.exitCode !== 0) {
      log.error(
        {
          telegram_user_id: input.telegramUserId,
          exit: r.exitCode,
          stderr: r.stderr.slice(-1000),
          stdout_tail: r.stdout.slice(-500),
        },
        "provisioner failed",
      );
      throw new Error(`provisioner exited ${r.exitCode}`);
    }
    log.info(
      { telegram_user_id: input.telegramUserId, elapsed_ms: Date.now() - t0 },
      "provisioning complete",
    );
    return findTenantByTelegramId(input.telegramUserId);
  })();

  inFlight.set(input.telegramUserId, work);
  try {
    return await work;
  } finally {
    inFlight.delete(input.telegramUserId);
  }
}
