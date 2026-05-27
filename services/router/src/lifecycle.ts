import { findIdleActiveTenants, markStatus } from "./db.js";
import { stopContainer } from "./docker.js";
import { config } from "./config.js";
import { log } from "./log.js";

/**
 * Hibernation tick — runs on an interval. Stops containers idle longer than
 * HIBERNATE_AFTER_MINUTES to free RAM. Containers auto-start on the next
 * incoming message (handled by webhook.ts → ensureRunning).
 */
export function startHibernationTick(): NodeJS.Timeout {
  const intervalMs = 60_000; // check every minute
  log.info(
    { threshold_min: config.HIBERNATE_AFTER_MINUTES, tick_ms: intervalMs },
    "hibernation tick started",
  );

  return setInterval(async () => {
    try {
      const idle = await findIdleActiveTenants(config.HIBERNATE_AFTER_MINUTES);
      for (const t of idle) {
        if (!t.container_name) continue;
        log.info(
          { tenant: t.id, name: t.container_name, idle_since: t.last_active_at },
          "hibernating idle tenant",
        );
        await stopContainer(t.container_name);
        await markStatus(t.id, "hibernated");
      }
    } catch (err) {
      log.error({ err }, "hibernation tick error");
    }
  }, intervalMs);
}
