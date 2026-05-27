import { z } from "zod";

// Validate-once-at-startup pattern. Fail loud if anything's missing.

const Env = z.object({
  // Server -----------------------------------------------------------
  PORT: z.coerce.number().int().min(1).max(65535).default(8080),
  LOG_LEVEL: z
    .enum(["fatal", "error", "warn", "info", "debug", "trace"])
    .default("info"),

  // Database (Supabase) ---------------------------------------------
  DATABASE_URL: z.string().url(),

  // Telegram --------------------------------------------------------
  TELEGRAM_BOT_TOKEN: z.string().min(20),
  // Optional: secret_token Telegram echoes on webhook header — guards
  // against forged webhooks. Set this when you call setWebhook.
  TELEGRAM_WEBHOOK_SECRET: z.string().min(8).optional(),

  // Docker ----------------------------------------------------------
  DOCKER_SOCKET: z.string().default("/var/run/docker.sock"),
  IMAGE_TAG: z.string().default("ghcr.io/antriksh-29/arete:latest"),

  // Tenant runtime --------------------------------------------------
  VOLUME_ROOT: z.string().default("/var/tenants"),
  PORT_POOL_START: z.coerce.number().int().default(9000),
  PORT_POOL_END: z.coerce.number().int().default(9999),
  HIBERNATE_AFTER_MINUTES: z.coerce.number().int().min(1).default(30),

  // Shared secrets injected into every tenant container -------------
  // Lifted from /etc/arete-secrets/* at runtime; env vars are the
  // dev-friendly path.
  SHARED_ANTHROPIC_KEY: z.string().optional(),
  SHARED_OPENAI_KEY: z.string().optional(),
  GITHUB_PAT: z.string().optional(), // for tenant repo clone

  // Admin -----------------------------------------------------------
  ADMIN_API_KEY: z.string().min(20).optional(),
});

export type Config = z.infer<typeof Env>;

export const config: Config = (() => {
  const parsed = Env.safeParse(process.env);
  if (!parsed.success) {
    console.error("[router] config validation failed:");
    console.error(parsed.error.flatten().fieldErrors);
    process.exit(2);
  }
  return parsed.data;
})();
