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
import { provisionNewTenant } from "./provision.js";
import {
  inferTimezoneFromCountry,
  renderUserMd,
  writeUserMdToContainer,
} from "./onboarding.js";

// --- First-time conversation (Q-K) ----------------------------------------
// We bypass the agent for the first 3 inbound messages so the user gets a
// fast scripted onboarding (no LLM cost, no cold-start) and we capture
// name + country into the DB before any model call. After 'complete', every
// message routes through openclaw agent normally.

function onboardingGreeting(): string {
  return [
    "👋 Hey! I'm Arete — your own personal AI assistant, living right here in Telegram.",
    "",
    "Quick 4-question setup so I can hit the ground running. First: what should I call you?",
  ].join("\n");
}

function onboardingAskCountry(name: string): string {
  return `Nice to meet you, ${name}. Which country are you based in? (gives me the right timezone for reminders + briefings.)`;
}

function onboardingAskOccupation(): string {
  return "Got it. What do you do? (one line is fine — e.g. \"founder of a SaaS startup\", \"product manager at a fintech\", \"med-school student\", \"freelance designer\")";
}

function onboardingAskUseCase(): string {
  return [
    "Last one: what would you most like to use me for? Pick the closest, or describe in your own words:",
    "",
    "1. Daily briefings — morning pull-together of calendar, inbox, open tasks",
    "2. Reminders — natural-language nudging and notifications",
    "3. Create skills — define what I can do for you, in plain English",
    "4. Knowledge brain — save anything you tell me, ask about it later",
    "5. Something else (just describe it)",
  ].join("\n");
}

// For a chosen use case, render a focused "Inputs + Outputs" explainer so the
// user immediately knows what to send AND what they'll get back. Avoids
// dumping the full capabilities catalog on the welcome turn — that surfaces
// only when the user asks ("what else can you do?", "help", etc.).
function pickStarterJourney(useCase: string): string {
  const t = useCase.toLowerCase();

  if (/\b(1|brief|morning|daily)/.test(t)) {
    return [
      "Daily briefings. A morning pull-together of what matters that day.",
      "",
      "Inputs — what you give me",
      "• What time you want it (e.g. \"send it at 7am\")",
      "• Permission to peek at your calendar + inbox when you connect them",
      "• Anything ad-hoc you want included (\"also include my reading list\")",
      "",
      "Outputs — what I send back",
      "• One Telegram message every morning at your chosen time",
      "• Top calendar events, important unread emails, open tasks, anything you flagged the night before",
      "",
      "Want to set it up now? Just tell me a time.",
    ].join("\n");
  }

  if (/\b(2|remind|nudg|notif)/.test(t)) {
    return [
      "Reminders. Tell me what to remember and when, in plain English.",
      "",
      "Inputs — what you give me",
      "• Anything like \"remind me to call Dad at 7pm\"",
      "• \"Remind me Monday morning to send the weekly update\"",
      "• \"Nudge me in 2 hours about the doctor's appointment\"",
      "",
      "Outputs — what I send back",
      "• A confirmation when I set it up (\"got it, I'll ping you at 7pm\")",
      "• A Telegram message at the right time with the reminder text",
      "",
      "Try one now — just type a reminder.",
    ].join("\n");
  }

  if (/\b(3|skill|create|build|capab)/.test(t)) {
    return [
      "Create skills. Teach me a new capability by describing it.",
      "",
      "Inputs — what you give me",
      "• A plain-English description of what you want me to be able to do",
      "  (e.g. \"every Sunday evening, review my week and ask what to focus on\")",
      "• Any details about timing, inputs, or where the data lives",
      "",
      "Outputs — what I do with it",
      "• I write the skill into your workspace, configure any schedule",
      "• I confirm what I set up and when it'll first fire",
      "• You can ask me to tweak or remove the skill anytime",
      "",
      "Tell me a skill you'd like me to build.",
    ].join("\n");
  }

  if (/\b(4|brain|memory|save|remember|knowledge)/.test(t)) {
    return [
      "Knowledge brain. A growing memory of anything you tell me.",
      "",
      "Inputs — what you give me",
      "• Any thought, fact, preference, or note (\"Note: <person> prefers Notion over Linear\")",
      "• Forwarded messages or links + \"save this\"",
      "• Questions about anything you've told me before",
      "",
      "Outputs — what I send back",
      "• Confirmation that I've filed it",
      "• When you ask, I resurface the right note with context",
      "",
      "Try it — tell me one thing you'd like me to remember.",
    ].join("\n");
  }

  // 5 / unmatched — open-ended.
  return [
    "Got it. Tell me a little more about what you'd want — even one example helps. Or just start using me and I'll figure out what you need over time.",
  ].join("\n");
}

function onboardingComplete(name: string, useCase: string): string {
  return [
    `Welcome aboard, ${name}. 🪶`,
    "",
    pickStarterJourney(useCase),
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
      // This message IS the country. Save it + infer timezone, ask occupation.
      const country = trimField(messageText);
      if (!country) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — which country are you based in?",
        );
        return true;
      }
      const timezone = inferTimezoneFromCountry(country);
      await setOnboarding(tenant.id, "awaiting_occupation", {
        country,
        timezone,
      });
      await sendPlainReply(chatId, onboardingAskOccupation());
      return true;
    }

    case "awaiting_occupation": {
      // This message IS the occupation. Save, ask use case.
      const occupation = trimField(messageText, 200);
      if (!occupation) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — one quick line about what you do?",
        );
        return true;
      }
      await setOnboarding(tenant.id, "awaiting_use_case", { occupation });
      await sendPlainReply(chatId, onboardingAskUseCase());
      return true;
    }

    case "awaiting_use_case": {
      // This message IS the use case. Save, mark complete, send welcome,
      // and render USER.md into the container so the agent has full
      // context from turn-1 after onboarding.
      const useCase = trimField(messageText, 500);
      if (!useCase) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — what would you most like to use me for? You can pick a number from 1-5 or describe it.",
        );
        return true;
      }
      await setOnboarding(tenant.id, "complete", { use_case: useCase });
      await sendPlainReply(
        chatId,
        onboardingComplete(tenant.name ?? "there", useCase),
      );
      // Write USER.md to the volume. Container needs to be running for
      // docker exec. Best-effort: if it fails (container hibernated,
      // exec hang), don't block the user — the file can be re-rendered
      // on the next inbound by checking onboarding_stage='complete' +
      // a sentinel marker. v2 cleanup.
      try {
        const userMd = renderUserMd({
          name: tenant.name,
          country: tenant.country,
          occupation: tenant.occupation,
          use_case: useCase,
          timezone: tenant.timezone,
        });
        // Ensure container is running before exec
        if (tenant.container_name) {
          await ensureRunning(tenant);
          await waitHealthy(tenant);
          await writeUserMdToContainer(tenant.container_name, userMd);
          log.info(
            { tenant: tenant.id },
            "wrote USER.md to volume",
          );
        }
      } catch (err) {
        log.warn(
          { err, tenant: tenant.id },
          "USER.md write failed — agent will use stub until next manual update",
        );
      }
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

import { sendChunkedMessage } from "./telegram.js";

async function sendPlainReply(chatId: number, text: string): Promise<void> {
  // Delegate to telegram.ts which handles 4096-char chunking + rate limit +
  // 429 retry. Wrapped in try/catch so a failed Telegram send doesn't crash
  // the webhook handler.
  try {
    await sendChunkedMessage(chatId, text);
  } catch (err) {
    log.warn({ err, chatId }, "failed to send chunked reply via Telegram");
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
    let tenant = await findTenantByTelegramId(from.id);

    if (!tenant) {
      // Unknown user — production signup flow. Send a "warming up" message
      // within ~3s so the user knows we received the request, then run the
      // provisioner (claims pool repo → clones to volume → docker create →
      // INSERT tenants row with onboarding_stage='awaiting_name'). After
      // provision, refetch the tenant and fall through to the onboarding
      // FSM, which sends the actual greeting + walks them through it.
      log.info(
        { telegram_user_id: from.id, username: from.username },
        "unknown user — auto-provisioning",
      );
      await sendPlainReply(
        chatId,
        "Setting up your assistant — give me ~30 seconds. I'll say hi properly once we're ready.",
      );
      try {
        tenant = await provisionNewTenant({
          telegramUserId: from.id,
          telegramUsername: from.username ?? null,
        });
      } catch (err) {
        log.error(
          { err, telegram_user_id: from.id },
          "auto-provision failed",
        );
        await sendPlainReply(
          chatId,
          "Hit a snag setting up your account. Try sending another message in a minute or two.",
        );
        return;
      }
      if (!tenant) {
        log.error(
          { telegram_user_id: from.id },
          "auto-provision returned no tenant row",
        );
        await sendPlainReply(
          chatId,
          "Hit a snag setting up your account. Try sending another message in a minute or two.",
        );
        return;
      }
      // Fall through to the FSM. The new tenant has onboarding_stage =
      // 'awaiting_name' so the FSM will send the greeting on this same
      // message turn (no second prompt needed).
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
