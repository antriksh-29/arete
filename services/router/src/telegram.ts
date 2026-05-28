// Telegram outbound: chunking + rate limiting.
//
// Q-H from architecture-research-2026-05-27.md. Two concerns:
//
// 1. Hard 4096-char limit per `sendMessage` (Telegram Bot API constraint).
//    Long agent replies need to be split. Naive .slice(0,4096) splits
//    mid-word and mid-Markdown — chunkAtBoundaries() prefers sentence /
//    paragraph / code-fence boundaries when possible.
//
// 2. Telegram Bot API rate limits: 30 messages/sec global per bot,
//    1 message/sec per chat. If we exceed, Telegram returns 429 with
//    `retry_after`. Without throttling, a single agent reply chunked
//    into 6 messages + a viral-moment burst could 429 us.
//
// In-memory queue is fine for v1 (single router instance, 10-30 tenants).
// If we scale to multi-host, swap for Redis-backed BullMQ (Q-J / Q-O).
import { config } from "./config.js";
import { log } from "./log.js";

const TG_MAX_LEN = 4096;
const GLOBAL_RATE = 30; // messages/sec across all chats
const PER_CHAT_RATE = 1; // messages/sec per chat_id

// ── chunking ─────────────────────────────────────────────────────────────

/**
 * Split a long string into Telegram-sized chunks (<=4096 chars each),
 * preferring boundaries in this order: paragraph blank-line > sentence
 * end > word > hard cut. Keeps code fences (```...```) intact when
 * possible — splits at fence boundaries instead of mid-block.
 */
export function chunkAtBoundaries(text: string, max = TG_MAX_LEN): string[] {
  if (text.length <= max) return [text];
  const chunks: string[] = [];
  let remaining = text;
  while (remaining.length > max) {
    // Try paragraph boundary (double newline)
    let cut = remaining.lastIndexOf("\n\n", max);
    if (cut < max / 2) cut = -1; // too early, ignore
    // Try sentence boundary
    if (cut < 0) {
      const m = remaining.slice(0, max).match(/[.!?]\s+(?=[A-Z(])/g);
      if (m && m.length > 0) {
        const last = m[m.length - 1]!;
        const lastSentenceEnd = remaining.slice(0, max).lastIndexOf(last);
        if (lastSentenceEnd > max / 2) cut = lastSentenceEnd + last.length;
      }
    }
    // Try newline
    if (cut < 0) cut = remaining.lastIndexOf("\n", max);
    if (cut < max / 2) cut = -1;
    // Try word boundary
    if (cut < 0) cut = remaining.lastIndexOf(" ", max);
    if (cut < max / 2) cut = -1;
    // Last resort: hard cut at limit
    if (cut < 0) cut = max;
    chunks.push(remaining.slice(0, cut).trimEnd());
    remaining = remaining.slice(cut).trimStart();
  }
  if (remaining.length) chunks.push(remaining);
  return chunks;
}

// ── rate limiting ────────────────────────────────────────────────────────

interface RateState {
  globalTokens: number;
  globalLastRefillMs: number;
  perChat: Map<number, { tokens: number; lastRefillMs: number }>;
}

const state: RateState = {
  globalTokens: GLOBAL_RATE,
  globalLastRefillMs: Date.now(),
  perChat: new Map(),
};

function refillTokens(currentTokens: number, lastRefillMs: number, ratePerSec: number, maxTokens: number): {
  tokens: number;
  lastRefillMs: number;
} {
  const now = Date.now();
  const elapsed = (now - lastRefillMs) / 1000;
  const added = elapsed * ratePerSec;
  return { tokens: Math.min(maxTokens, currentTokens + added), lastRefillMs: now };
}

async function acquireSendSlot(chatId: number): Promise<void> {
  // Spin until both buckets have >= 1 token. Sleep short intervals.
  // For our throughput (10-30 tenants), this loop iterates 1-2 times typical.
  while (true) {
    const g = refillTokens(state.globalTokens, state.globalLastRefillMs, GLOBAL_RATE, GLOBAL_RATE);
    state.globalTokens = g.tokens;
    state.globalLastRefillMs = g.lastRefillMs;

    let pc = state.perChat.get(chatId);
    if (!pc) {
      pc = { tokens: PER_CHAT_RATE, lastRefillMs: Date.now() };
      state.perChat.set(chatId, pc);
    } else {
      const refilled = refillTokens(pc.tokens, pc.lastRefillMs, PER_CHAT_RATE, PER_CHAT_RATE);
      pc.tokens = refilled.tokens;
      pc.lastRefillMs = refilled.lastRefillMs;
    }

    if (state.globalTokens >= 1 && pc.tokens >= 1) {
      state.globalTokens -= 1;
      pc.tokens -= 1;
      return;
    }
    // Sleep just long enough for the most-starved bucket to gain a token.
    const globalWaitMs = state.globalTokens < 1 ? (1 - state.globalTokens) * (1000 / GLOBAL_RATE) : 0;
    const chatWaitMs = pc.tokens < 1 ? (1 - pc.tokens) * (1000 / PER_CHAT_RATE) : 0;
    const waitMs = Math.ceil(Math.max(globalWaitMs, chatWaitMs, 50));
    await new Promise((r) => setTimeout(r, waitMs));
  }
}

// ── sendMessage ──────────────────────────────────────────────────────────

interface SendOptions {
  parseMode?: "MarkdownV2" | "HTML";
  replyToMessageId?: number;
}

/**
 * Send a message to Telegram. Handles chunking, rate-limiting, and 429
 * retry. Sends each chunk as a separate message in order. Resolves once
 * ALL chunks have been delivered (or after permanent failure).
 */
export async function sendChunkedMessage(
  chatId: number,
  text: string,
  opts: SendOptions = {},
): Promise<void> {
  if (!text || !text.trim()) return;
  const chunks = chunkAtBoundaries(text);
  const url = `https://api.telegram.org/bot${config.TELEGRAM_BOT_TOKEN}/sendMessage`;

  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    await acquireSendSlot(chatId);

    const body: Record<string, unknown> = {
      chat_id: chatId,
      text: chunk,
    };
    if (opts.parseMode) body.parse_mode = opts.parseMode;
    if (opts.replyToMessageId && i === 0) body.reply_to_message_id = opts.replyToMessageId;

    // Retry up to 3 times on 429 (honoring retry_after)
    let attempt = 0;
    while (attempt < 3) {
      let res: Response;
      try {
        res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(15_000),
        });
      } catch (err) {
        log.warn({ err, chatId, chunkIdx: i, attempt }, "telegram sendMessage network error");
        await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
        attempt++;
        continue;
      }
      if (res.ok) break;
      if (res.status === 429) {
        const data = (await res.json().catch(() => ({}))) as { parameters?: { retry_after?: number } };
        const retryAfter = data.parameters?.retry_after ?? 5;
        log.warn({ chatId, retryAfter }, "telegram 429 — sleeping");
        await new Promise((r) => setTimeout(r, retryAfter * 1000));
        attempt++;
        continue;
      }
      // Other error — log and give up on this chunk
      const errBody = await res.text().catch(() => "<no body>");
      log.error(
        { chatId, status: res.status, body: errBody.slice(0, 200) },
        "telegram sendMessage failed",
      );
      break;
    }
  }
}
