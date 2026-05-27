import type { Request, Response, NextFunction } from "express";
import { listAllTenants } from "./db.js";
import { listAreteContainers } from "./docker.js";
import { config } from "./config.js";

export function requireAdmin(
  req: Request,
  res: Response,
  next: NextFunction,
): void {
  if (!config.ADMIN_API_KEY) {
    res.status(503).send("admin disabled (ADMIN_API_KEY not configured)");
    return;
  }
  if (req.header("X-Admin-Key") !== config.ADMIN_API_KEY) {
    res.status(401).send("unauthorized");
    return;
  }
  next();
}

export async function getTenants(_req: Request, res: Response): Promise<void> {
  const [tenants, containers] = await Promise.all([
    listAllTenants(),
    listAreteContainers(),
  ]);
  const byName = new Map(containers.map((c) => [c.name, c]));
  const enriched = tenants.map((t) => ({
    ...t,
    container_state: t.container_name ? byName.get(t.container_name) ?? null : null,
  }));
  res.json({ count: enriched.length, tenants: enriched });
}
