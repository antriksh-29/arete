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
    "2. Email triage — read, draft, schedule replies",
    "3. Reminders + habits — natural-language scheduling, recurring nudges",
    "4. Knowledge brain — save anything I tell you, ask about it later",
    "5. Meeting prep + scheduling",
    "6. Research + summaries — articles, PDFs, YouTube, X threads",
    "7. Something else (just describe it)",
  ].join("\n");
}

// Map a free-text use-case response to a starter-journey nudge. Best-effort
// substring detection — if nothing matches, we fall back to the full catalog.
function pickStarterJourney(useCase: string): string {
  const t = useCase.toLowerCase();
  if (/\b(1|brief|morning|daily)/.test(t)) {
    return [
      "Perfect — let's set up your daily morning briefing.",
      "",
      "I'll need a couple of things to make this work:",
      "• What time should I send it? (e.g. \"7am\")",
      "• Should I pull from Gmail + Google Calendar? (I'll ask for permission separately)",
      "",
      "Just answer those two and I'll wire it up.",
    ].join("\n");
  }
  if (/\b(2|email|triage|inbox)/.test(t)) {
    return [
      "Great choice — email triage is one of the most impactful things I can do.",
      "",
      "First step: I'll need permission to connect to your Gmail. When you're ready, just say \"connect Gmail\" and I'll walk you through it.",
      "",
      "Once connected, you can ask me things like:",
      "• \"What's unread that matters?\"",
      "• \"Draft a reply to <person>\"",
      "• \"Mark everything from <newsletter> as read\"",
    ].join("\n");
  }
  if (/\b(3|remind|habit|nudge)/.test(t)) {
    return [
      "Awesome — reminders and habits are easy. Try these right now:",
      "",
      "• \"Remind me to call Dad at 7pm\"",
      "• \"Every weekday at 9am, ask if I drank water\"",
      "• \"Remind me Monday morning to send the weekly update\"",
      "",
      "I'll set them up and check in at the right time.",
    ].join("\n");
  }
  if (/\b(4|brain|memory|save|remember|knowledge)/.test(t)) {
    return [
      "Got it — let's make the brain useful right away. Try one of these:",
      "",
      "• Forward me any message and say \"save this\" — I'll file it.",
      "• \"Note: <person> prefers Notion over Linear\" — I'll remember.",
      "• \"What did <person> say about pricing last week?\" — I'll look it up.",
      "",
      "The more you tell me, the sharper I get at resurfacing it later.",
    ].join("\n");
  }
  if (/\b(5|meeting|schedul|calendar)/.test(t)) {
    return [
      "Meeting prep is a great fit. First, I'll need permission to connect to Google Calendar — say \"connect Calendar\" when you're ready.",
      "",
      "Once connected, you can ask:",
      "• \"What's on for tomorrow?\"",
      "• \"Prep me for the 3pm with <person>\" — I'll pull recent emails + notes",
      "• \"Find 30 min next week with <person>\"",
    ].join("\n");
  }
  if (/\b(6|research|summar|article|pdf|youtube|x[\s-]?thread)/.test(t)) {
    return [
      "Research is one of my favourites. Try:",
      "",
      "• Send me a YouTube link — \"summarise this\"",
      "• Forward an article URL — I'll pull the gist + file it to your brain",
      "• Send a PDF — I'll extract the key takeaways",
      "• Send an X (Twitter) thread link — I'll save it as a single page",
    ].join("\n");
  }
  // 7 / unmatched — let the user explore.
  return [
    "Got it. Whatever it is, I'll try to make it work. Tell me more about how you'd like to use me, or just start asking — I'll figure it out.",
  ].join("\n");
}

function onboardingComplete(name: string, useCase: string): string {
  return [
    `Welcome aboard, ${name}. 🪶`,
    "",
    pickStarterJourney(useCase),
    "",
    "─────────",
    "**Everything else I can do** — for whenever you want to explore:",
    "",
    "📅 **Daily briefings** — \"brief me on my day\"",
    "✉️ **Email** — read, draft, schedule replies (needs Gmail permission)",
    "🗓️ **Calendar** — meeting prep, scheduling, find time (needs Calendar permission)",
    "⏰ **Reminders + habits** — \"remind me to X\", \"every morning ask Y\"",
    "🧠 **Knowledge brain** — \"save this\", \"what did X say about Y\"",
    "📄 **Research + summaries** — PDFs, articles, YouTube, X threads",
    "📝 **Notes + lists** — \"add to my reading list\", \"track my water intake\"",
    "🔍 **Web search** — \"google X for me\"",
    "🎙️ **Voice in / out** — send me a voice note, I'll transcribe + reply",
    "",
    "Just talk to me — I'll figure out which one you mean.",
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
      // This message IS the country. Save it, ask occupation.
      const country = trimField(messageText);
      if (!country) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — which country are you based in?",
        );
        return true;
      }
      await setOnboarding(tenant.id, "awaiting_occupation", { country });
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
      // This message IS the use case. Save, mark complete, send welcome
      // with a starter-journey nudge tailored to the use case.
      const useCase = trimField(messageText, 500);
      if (!useCase) {
        await sendPlainReply(
          chatId,
          "Didn't catch that — what would you most like to use me for? You can pick a number from 1-7 or describe it.",
        );
        return true;
      }
      await setOnboarding(tenant.id, "complete", { use_case: useCase });
      await sendPlainReply(
        chatId,
        onboardingComplete(tenant.name ?? "there", useCase),
      );
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
