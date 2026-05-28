import type { Request, Response } from "express";
import {
  findTenantByTelegramId,
  touchLastActive,
  markStatus,
  setOnboarding,
  type Tenant,
} from "./db.js";
import { ensureRunning, waitHealthy, runAgentTurn } from "./docker.js";
import { config } from "./config.js";
import { log } from "./log.js";

// --- First-time conversation (Q-K) ----------------------------------------
// We bypass the agent for the first 3 inbound messages so the user gets a
// fast scripted onboarding (no LLM cost, no cold-start) and we capture
// name + country into the DB before any model call. After 'complete', every
// message routes through openclaw agent normally.

function onboardingGreeting(): string {
  return [
    "👋 Hey! I'm Arete — your own personal AI assistant living right here in Telegram.",
    "",
    "Before we dive in, what's your first name?",
  ].join("\n");
}

function onboardingAskCountry(name: string): string {
  return `Nice to meet you, ${name}. Which country are you based in? (so I can get your timezone right for reminders + briefings.)`;
}

function onboardingComplete(name: string): string {
  return [
    `Great, all set ${name}. Here are a few things you can ask me right away:`,
    "",
    "• \"Brief me on my day\" — pulls your calendar + inbox + open tasks.",
    "• \"Remind me to call Dad at 7pm\" — natural-language scheduling.",
    "• \"Add water-log to my habits\" — I track recurring habits + nudge you.",
    "• \"Save this thread\" — I remember anything you tell me; ask me about it any time.",
    "• \"What's on for tomorrow?\" — calendar-aware planning.",
    "",
    "Try anything — I'll figure it out. Ask me a question?",
  ].join("\n");
}

// Sanitize a free-text reply into a short field value. Telegram messages can
// be long; first-name should be short. Country same. Keeps DB tidy.
function trimField(s: string, max = 64): string {
  return s.trim().replace(/\s+/g, " ").slice(0, max);
}

/**
 * Handle one inbound message under the onboarding FSM. Returns true if the
 * router consumed the message (sent its own reply, no agent call needed).
 * Returns false if the message should be routed to the agent.
 *
 * State transitions are triggered by the USER's message, not the bot's reply.
 * The 4-state FSM is documented in db/migrations/0002_onboarding.sql.
 */
async function handleOnboarding(
  tenant: Tenant,
  messageText: string,
  chatId: number,
): Promise<boolean> {
  switch (tenant.onboarding_stage) {
    case "complete":
      return false;

    case "awaiting_name": {
      // First inbound from this tenant. Greet + ask name. Advance to 'greeted'
      // so the next message is interpreted as their name.
      await setOnboarding(tenant.id, "greeted");
      await sendPlainReply(chatId, onboardingGreeting());
      return true;
    }

    case "greeted": {
      // This message IS the user's name. Save it, ask country.
      const name = trimField(messageText);
      if (!name) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — what should I call you?",
        );
        return true;
      }
      await setOnboarding(tenant.id, "awaiting_country", { name });
      await sendPlainReply(chatId, onboardingAskCountry(name));
      return true;
    }

    case "awaiting_country": {
      // This message IS the country. Save it, send welcome, mark complete.
      const country = trimField(messageText);
      if (!country) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — which country are you based in?",
        );
        return true;
      }
      await setOnboarding(tenant.id, "complete", { country });
      await sendPlainReply(chatId, onboardingComplete(tenant.name ?? "there"));
      return true;
    }
  }
}

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

    // Touch last_active_at IMMEDIATELY so the hibernation tick doesn't decide
    // this tenant is idle and kill the container while an agent turn is still
    // in flight.
    await touchLastActive(tenant.id);

    // No message text? Skip agent turn. Telegram already got 200.
    const messageText = update.message?.text;
    if (!messageText) {
      log.debug({ update_id: update.update_id }, "non-text update — skipping agent turn");
      return;
    }

    // First-time conversation: bypass the agent for the scripted onboarding
    // (Q-K in docs/architecture-research-2026-05-27.md). Saves cold-start
    // latency + LLM cost on the first 3 turns.
    if (tenant.onboarding_stage !== "complete") {
      log.info(
        { tenant: tenant.id, stage: tenant.onboarding_stage },
        "router-side onboarding turn",
      );
      const consumed = await handleOnboarding(tenant, messageText, chatId);
      if (consumed) return;
      // If consumed=false somehow (shouldn't happen unless stage=complete and
      // we missed the early return) fall through to agent.
    }

    // Ensure container is up + healthy (only for non-onboarding turns; saves
    // the cold-start cost during onboarding).
    await ensureRunning(tenant);
    if (tenant.status === "hibernated") {
      await markStatus(tenant.id, "active");
    }
    await waitHealthy(tenant);

    log.info(
      { tenant: tenant.id, telegram_user_id: from.id, msg_len: messageText.length },
      "running agent turn",
    );
    const reply = await runAgentTurn(tenant, messageText, from.id);
    if (reply && reply.trim()) {
      log.info(
        { tenant: tenant.id, reply_len: reply.length },
        "sending reply to telegram",
      );
      await sendPlainReply(chatId, reply);
    } else {
      log.warn({ tenant: tenant.id }, "agent returned empty reply — nothing to send");
    }
    // Touch again at the end so idle calculation reflects turn-completion,
    // not turn-start (matters for back-to-back conversation flows).
    await touchLastActive(tenant.id);
  } catch (err) {
    log.error({ err, telegram_user_id: from.id }, "webhook handling failed");
    await sendPlainReply(
      chatId,
      "Something hiccupped on my end. Try again in a moment.",
    );
  }
}
