import type { Request, Response } from "express";
import { findTenantByTelegramId, touchLastActive, markStatus } from "./db.js";
import { ensureRunning, waitHealthy } from "./docker.js";
import { proxyWebhook } from "./proxy.js";
import { config } from "./config.js";
import { log } from "./log.js";

interface TelegramFromUser {
  id: number;
  username?: string;
  first_name?: string;
  last_name?: string;
}

interface TelegramUpdate {
  update_id: number;
  message?: {
    chat?: { id: number };
    from?: TelegramFromUser;
    text?: string;
  };
  edited_message?: {
    chat?: { id: number };
    from?: TelegramFromUser;
  };
  callback_query?: {
    from?: TelegramFromUser;
  };
}

function extractFromUser(u: TelegramUpdate): TelegramFromUser | null {
  return (
    u.message?.from ?? u.edited_message?.from ?? u.callback_query?.from ?? null
  );
}

async function sendPlainReply(chatId: number, text: string): Promise<void> {
  const url = `https://api.telegram.org/bot${config.TELEGRAM_BOT_TOKEN}/sendMessage`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text }),
      signal: AbortSignal.timeout(10_000),
    });
  } catch (err) {
    log.warn({ err, chatId }, "failed to send plain reply via Telegram");
  }
}

export async function handleWebhook(req: Request, res: Response): Promise<void> {
  // 1. Verify webhook secret if configured
  if (config.TELEGRAM_WEBHOOK_SECRET) {
    const got = req.header("X-Telegram-Bot-Api-Secret-Token");
    if (got !== config.TELEGRAM_WEBHOOK_SECRET) {
      log.warn({ ip: req.ip }, "webhook secret mismatch — rejecting");
      res.status(401).send("unauthorized");
      return;
    }
  }

  // 2. Acknowledge fast (Telegram retries on slow responses)
  res.status(200).send("ok");

  // 3. Process out-of-band so the response is already out
  const update = req.body as TelegramUpdate;
  const from = extractFromUser(update);
  const chatId = update.message?.chat?.id ?? update.edited_message?.chat?.id;

  if (!from || !chatId) {
    log.debug({ update_id: update.update_id }, "webhook with no from/chat — ignoring");
    return;
  }

  try {
    const tenant = await findTenantByTelegramId(from.id);

    if (!tenant) {
      // Unknown user — friendly response, no provisioning yet (Phase D).
      log.info(
        { telegram_user_id: from.id, username: from.username },
        "unknown user — sending beta-gate reply",
      );
      await sendPlainReply(
        chatId,
        "👋 You're not on the Arete beta yet. Ping @antriksh_29 if you'd like access.",
      );
      return;
    }

    if (tenant.status === "suspended") {
      await sendPlainReply(
        chatId,
        "This account is paused. Please contact support.",
      );
      return;
    }

    if (tenant.status === "failed") {
      await sendPlainReply(
        chatId,
        "Something went wrong setting up your account. Try again in a few minutes or ping support.",
      );
      return;
    }

    // Ensure container is up + healthy
    await ensureRunning(tenant);
    if (tenant.status === "hibernated") {
      await markStatus(tenant.id, "active");
    }
    await waitHealthy(tenant);

    // Forward the webhook to the container
    await proxyWebhook(tenant, update);
    await touchLastActive(tenant.id);
  } catch (err) {
    log.error({ err, telegram_user_id: from.id }, "webhook handling failed");
    await sendPlainReply(
      chatId,
      "Something hiccupped on my end. Try again in a moment.",
    );
  }
}
