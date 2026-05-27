import express from "express";
import { pinoHttp } from "pino-http";
import { config } from "./config.js";
import { log } from "./log.js";
import { handleWebhook } from "./webhook.js";
import { getTenants, requireAdmin } from "./admin.js";
import { startHibernationTick } from "./lifecycle.js";
import { closeDb } from "./db.js";

const app = express();

app.use(express.json({ limit: "1mb" }));
app.use(pinoHttp({ logger: log }));

// Health for the host's monitoring + Caddy
app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "arete-router", uptime_s: process.uptime() });
});

// Telegram webhook — single endpoint, the platform's only public surface
app.post("/telegram/webhook", handleWebhook);

// Admin (auth-gated; disabled if ADMIN_API_KEY not set)
app.get("/admin/tenants", requireAdmin, getTenants);

// Last-resort error handler
app.use((err: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  log.error({ err }, "unhandled error");
  res.status(500).send("internal error");
});

const server = app.listen(config.PORT, "0.0.0.0", () => {
  log.info({ port: config.PORT, env: process.env.NODE_ENV ?? "production" }, "router up");
});

const hibernationTimer = startHibernationTick();

// Graceful shutdown
let shuttingDown = false;
async function shutdown(signal: string): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  log.info({ signal }, "shutting down");
  clearInterval(hibernationTimer);
  server.close();
  await closeDb();
  process.exit(0);
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
