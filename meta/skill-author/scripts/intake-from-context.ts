#!/usr/bin/env -S bun run
/**
 * intake-from-context.ts — LLM-driven shortcut around the Q1-Q5 elicit flow.
 *
 * Triggered when the user signals "yes implement this" / "let's build it"
 * AFTER an established chat about a feature (criteria, format, schedule,
 * sample output already discussed). Goal: don't make the user re-answer
 * five questions they've effectively already answered.
 *
 * Flow:
 *   1. Read recent OpenClaw session messages from the jsonl file (the
 *      same source the agent itself sees — verbatim chat, no agent
 *      paraphrasing risk).
 *   2. Single moonshot-v1-32k call extracts {capability, invocation_mode,
 *      triggers, schedule, output} from the conversation.
 *   3. Save a partial-or-full Draft to .context/skill-author-drafts/.
 *   4. Spawn elicit.ts --mode=resume --slug=<draft> which:
 *        - emits the bullet summary (if all 5 slots filled), or
 *        - asks the next missing question (if 1-4 slots filled).
 *
 * On any extraction failure (LLM 5xx, malformed JSON, validation fail),
 * fall through to a clean elicit.ts --mode=create — equivalent to today's
 * UX. Never worse than the baseline.
 *
 * Usage:
 *   bun run intake-from-context.ts --mode=create-oneshot
 *     --message="<user's verbatim latest message>"
 *     [--session-file=<path>]   # auto-detected from $HOME/.openclaw if absent
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "fs";
import { join, resolve } from "path";

// ──────────────────────────────────────────────────────────────────
// Types (mirror elicit.ts Draft shape so the existing pipeline accepts our output)
// ──────────────────────────────────────────────────────────────────

interface CreateAnswers {
  capability?: string;
  invocation_mode?: "manual" | "periodic";
  triggers?: string[];
  schedule?: {
    frequency: "hourly" | "daily" | "weekly" | "monthly" | "custom";
    time_of_day?: string;
    day_of_week?: string;
    day_of_month?: number;
    custom_cron?: string;
    timezone?: string;
  };
  output?: string;
}

interface Draft {
  schema_version: 1;
  slug: string;
  mode: "create";
  sub_mode?: "intake-from-context";
  created_at: string;
  updated_at: string;
  completed_questions: number[];
  awaiting_confirmation?: boolean;
  answers: CreateAnswers;
  // Audit trail of what the LLM was told and what it extracted, useful
  // for debugging hallucinated fields. Stored on disk; never shown to user.
  intake_audit?: {
    session_file: string;
    messages_used: number;
    llm_extracted: Partial<CreateAnswers>;
    llm_missing: string[];
    llm_confidence: Record<string, "high" | "medium" | "low">;
  };
}

interface Envelope {
  ok: boolean;
  phase: "elicit" | "error";
  user_message: string;
  draft_slug: string | null;
  detail?: Record<string, unknown>;
}

interface ChatMessage {
  role: "user" | "assistant" | "system" | "tool" | string;
  content: string;
  timestamp: string;
}

// ──────────────────────────────────────────────────────────────────
// Constants
// ──────────────────────────────────────────────────────────────────

const REPO_ROOT = process.env.REPO_ROOT ?? resolve(__dirname, "../../..");
const DRAFT_DIR = join(REPO_ROOT, ".context", "skill-author-drafts");
const SESSIONS_DIR = process.env.OPENCLAW_SESSIONS_DIR
  ?? join(process.env.HOME ?? "", ".openclaw", "agents", "main", "sessions");
const RECENT_MESSAGES = 30;       // how many tail messages to feed the LLM
const SESSION_FRESH_MIN = 60;     // only consider session files modified within last hour
// Use the absolute bun binary path — when this script is spawned as a
// subprocess (e.g. from the agent's exec), the inherited PATH may not
// include ~/.bun/bin, so resolving via "bun" alone fails (verified
// 2026-05-21: "Executable not found in $PATH: 'bun'").
const BUN_BIN = process.env.BUN_INSTALL
  ? join(process.env.BUN_INSTALL, "bin", "bun")
  : join(process.env.HOME ?? "", ".bun", "bin", "bun");

// ──────────────────────────────────────────────────────────────────
// CLI
// ──────────────────────────────────────────────────────────────────

function parseArgs(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const arg of argv.slice(2)) {
    if (!arg.startsWith("--")) continue;
    const eq = arg.indexOf("=");
    if (eq === -1) out[arg.slice(2)] = true;
    else out[arg.slice(2, eq)] = arg.slice(eq + 1);
  }
  return out;
}

// ──────────────────────────────────────────────────────────────────
// Envelope helpers
// ──────────────────────────────────────────────────────────────────

function emit(env: Envelope): never {
  console.log(JSON.stringify(env));
  process.exit(0);
}

function fallbackToFreshElicit(reason: string): never {
  // Spawn a clean elicit.ts --mode=create and pipe its envelope through.
  // This is the "no worse than today" guarantee — if intake fails for any
  // reason, the user gets the existing Q1 flow.
  const elicitPath = join(REPO_ROOT, "meta", "skill-author", "scripts", "elicit.ts");
  const proc = Bun.spawnSync({
    cmd: [BUN_BIN, "run", elicitPath, "--mode=create", "--force-new"],
    env: { ...process.env, REPO_ROOT },
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = proc.stdout.toString().trim();
  const stderr = proc.stderr.toString().trim();
  if (stdout) {
    // Pass elicit's envelope through verbatim so the agent relays Q1.
    // (We deliberately drop the fallback "reason" for the user — they
    // get the same UX as a normal /teach, no debug noise.)
    console.log(stdout);
    process.exit(0);
  }
  emit({
    ok: false,
    phase: "error",
    user_message: `Couldn't set up the new skill (${reason.slice(0, 80)}). Try \`/teach\` to start fresh.`,
    draft_slug: null,
    detail: { fallback: "fresh_elicit_failed", elicit_stderr: stderr.slice(0, 300) },
  });
}

// ──────────────────────────────────────────────────────────────────
// Session jsonl reader (D1 = read OpenClaw session directly)
// ──────────────────────────────────────────────────────────────────

function findActiveSessionFile(): string | null {
  // The session file is the most recently modified jsonl in SESSIONS_DIR
  // that's not a trajectory/reset/checkpoint variant.
  if (!existsSync(SESSIONS_DIR)) return null;
  type Cand = { path: string; mtime: number };
  const candidates: Cand[] = [];
  for (const f of readdirSync(SESSIONS_DIR)) {
    if (!f.endsWith(".jsonl")) continue;
    if (f.includes(".trajectory.") || f.includes(".reset.") || f.includes(".checkpoint.")) continue;
    const p = join(SESSIONS_DIR, f);
    try {
      const st = statSync(p);
      candidates.push({ path: p, mtime: st.mtimeMs });
    } catch { /* skip */ }
  }
  if (!candidates.length) return null;
  candidates.sort((a, b) => b.mtime - a.mtime);
  const freshest = candidates[0];
  const ageMin = (Date.now() - freshest.mtime) / 60000;
  if (ageMin > SESSION_FRESH_MIN) return null;
  return freshest.path;
}

function readSessionTail(sessionFile: string, limit: number): ChatMessage[] {
  if (!existsSync(sessionFile)) return [];
  let raw: string;
  try {
    raw = readFileSync(sessionFile, "utf8");
  } catch {
    return [];
  }
  const out: ChatMessage[] = [];
  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line);
      const msg = entry?.message;
      if (!msg || !msg.role) continue;
      let content = msg.content;
      // OpenClaw stores content as either string OR array-of-parts
      if (Array.isArray(content)) {
        const textParts: string[] = [];
        for (const p of content) {
          if (p?.type === "text" && typeof p.text === "string") {
            textParts.push(p.text);
          }
          // skip toolCall / toolResult / image parts — too noisy for intake
        }
        content = textParts.join("\n");
      }
      if (typeof content !== "string" || !content.trim()) continue;
      // Skip "Conversation info (untrusted metadata)" wrappers — those are
      // the OpenClaw chat-id/sender envelopes, not actual user prose.
      if (content.startsWith("Conversation info (untrusted metadata)")) continue;
      if (content.startsWith("Sender (untrusted metadata)")) continue;
      // Truncate any single message to ~2000 chars so a giant pasted log
      // doesn't blow the context window.
      if (content.length > 2000) content = content.slice(0, 2000) + "...[truncated]";
      out.push({
        role: msg.role,
        content,
        timestamp: entry?.timestamp ?? "",
      });
    } catch { /* skip malformed line */ }
  }
  // Return only the last `limit` entries
  return out.slice(-limit);
}

// ──────────────────────────────────────────────────────────────────
// LLM extraction (Moonshot v1-32k with response_format=json_object)
// ──────────────────────────────────────────────────────────────────

async function readVaultKey(name: string): Promise<string | null> {
  try {
    const child = Bun.spawnSync(["bot-cred", "get", name]);
    const out = child.stdout.toString().trim();
    if (out && !out.startsWith("error")) return out;
    return null;
  } catch {
    return null;
  }
}

const INTAKE_SYSTEM_PROMPT = `You read a recent Telegram chat between Alfred (a personal assistant bot) and his user, plus the user's latest message, then extract the structured fields needed to create a new automated skill.

Two possible shapes of the latest message:

  (a) "Substantive first message" — the user opened the conversation with a detailed spec (multiple sentences, mentions schedule, output format, channels, etc.). In this case the chat history may be empty or unrelated. EXTRACT FROM THE LATEST MESSAGE ITSELF.

  (b) "Confirmation after chat" — the latest message is short ("yes implement this", "let's build it", "ship it"). The actual spec is in the chat history above. EXTRACT FROM THE CHAT HISTORY.

In both cases, you treat the union of {chat history + latest message} as the source of truth. Prefer the latest message's literal wording when it conflicts with chat history (e.g. user said "actually 6:30pm not 6:15pm" — use 6:30pm).

Your job: extract these fields from the conversation. Return ONLY the JSON object specified below. Use null for fields you cannot confidently determine from chat context.

Fields:
  • capability (string): One-sentence summary of what the skill enables. Write it the way Alfred would: imperative, concrete, no marketing fluff. E.g. "Send a daily YC launch digest at 6:30pm IST".
  • invocation_mode ("manual" | "periodic" | null): "periodic" if the user wants it on a schedule (daily, every morning, every hour, at X time, etc). "manual" if the user wants to trigger it themselves with words.
  • triggers (array of strings or null): When invocation_mode="manual", the trigger phrases (2-5 short phrases the user would type). Null for periodic.
  • schedule (object or null): When invocation_mode="periodic", the schedule shape:
      { frequency: "hourly"|"daily"|"weekly"|"monthly", time_of_day: "HH:MM" (24h, IST assumed), day_of_week?: "monday".."sunday", day_of_month?: 1-28 }
    Null for manual.
  • output (string): How the result is delivered. E.g. "Telegram chat message", "two-line message: bullet list + source URL", "email to me".
  • missing (array of strings): Fields you couldn't determine. Use the field name. E.g. ["triggers"] if it's manual but the user never named trigger phrases.
  • confidence (object): For each field you DID fill, your confidence: "high" / "medium" / "low".

CRITICAL RULES:
  1. If the chat already settled the schedule (e.g. user said "6:15pm" and bot confirmed), USE that — do NOT mark it missing.
  2. If the user CORRECTED a value mid-conversation (e.g. "actually 6:30pm not 6:15pm"), use the corrected value.
  3. If the user used relative time ("evening", "morning"), translate to a concrete HH:MM only if the chat made it concrete; otherwise mark schedule missing.
  4. Default timezone is Asia/Kolkata (IST) for this user.
  5. NEVER hallucinate. If the chat doesn't mention output format, mark output missing.
  6. Return raw JSON. Start with \`{\`, end with \`}\`. No markdown fences, no prose.

Example output for the YC daily report case:
{
  "capability": "Deliver a daily 6:30pm digest of YC company launches and announcements from the last 24 hours, as a bullet list with one-line descriptions and YC post URLs.",
  "invocation_mode": "periodic",
  "triggers": null,
  "schedule": { "frequency": "daily", "time_of_day": "18:30" },
  "output": "Telegram chat message: bullet list, each line has company name, one-line description, and the YC launch URL.",
  "missing": [],
  "confidence": { "capability": "high", "invocation_mode": "high", "schedule": "high", "output": "high" }
}`;

interface Extraction {
  capability: string | null;
  invocation_mode: "manual" | "periodic" | null;
  triggers: string[] | null;
  schedule: CreateAnswers["schedule"] | null;
  output: string | null;
  missing: string[];
  confidence: Record<string, "high" | "medium" | "low">;
}

function extractFirstJSON(text: string): Record<string, any> | null {
  if (!text) return null;
  let cleaned = text;
  const fence = cleaned.match(/```(?:json|JSON|javascript|js)?\s*\n([\s\S]+?)\n```/);
  if (fence && fence[1]) cleaned = fence[1];
  const start = cleaned.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < cleaned.length; i++) {
    const ch = cleaned[i];
    if (escape) { escape = false; continue; }
    if (ch === "\\") { escape = true; continue; }
    if (ch === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(cleaned.slice(start, i + 1)); } catch { return null; }
      }
    }
  }
  return null;
}

async function callKimiForExtraction(
  messages: ChatMessage[],
  latestMessage: string,
): Promise<{ ok: boolean; extraction?: Extraction; error?: string }> {
  const apiKey = process.env.MOONSHOT_API_KEY ?? await readVaultKey("MOONSHOT_API_KEY");
  if (!apiKey) return { ok: false, error: "MOONSHOT_API_KEY not available" };

  const userPayload = {
    recent_chat: messages.map(m => ({
      role: m.role,
      content: m.content,
    })),
    latest_user_message: latestMessage,
    note: "The latest message is the user signaling readiness to implement. Extract the skill spec from the chat ABOVE it.",
  };

  const userMessage = `Extract the skill spec from this conversation and return the JSON envelope per the system prompt.\n\n\`\`\`json\n${JSON.stringify(userPayload, null, 2)}\n\`\`\``;

  const PER_REQUEST_TIMEOUT_MS = 90_000;
  const TOTAL_BUDGET_MS = 120_000;
  let lastError = "";
  const start = Date.now();
  while (Date.now() - start < TOTAL_BUDGET_MS) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), PER_REQUEST_TIMEOUT_MS);
    try {
      const r = await fetch("https://api.moonshot.ai/v1/chat/completions", {
        method: "POST",
        signal: ctrl.signal,
        headers: {
          "Authorization": `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: "moonshot-v1-32k",
          messages: [
            { role: "system", content: INTAKE_SYSTEM_PROMPT },
            { role: "user", content: userMessage },
          ],
          response_format: { type: "json_object" },
          max_tokens: 4000,
          temperature: 0.2,
        }),
      });
      if (r.status >= 500) {
        clearTimeout(t);
        lastError = `${r.status}`;
        await new Promise(res => setTimeout(res, 1500));
        continue;
      }
      if (!r.ok) {
        clearTimeout(t);
        return { ok: false, error: `Moonshot API ${r.status}: ${(await r.text()).slice(0, 300)}` };
      }
      const body = await r.json() as {
        choices?: { message?: { content?: string; reasoning_content?: string } }[];
      };
      const content = body.choices?.[0]?.message?.content ?? "";
      const reasoning = body.choices?.[0]?.message?.reasoning_content ?? "";
      let json = content ? extractFirstJSON(content) : null;
      if (!json && reasoning) json = extractFirstJSON(reasoning);
      clearTimeout(t);
      if (!json) {
        return { ok: false, error: `Could not parse JSON from Moonshot reply (content=${content.length}b reasoning=${reasoning.length}b)` };
      }
      // Loose validation; the LLM can return null for any field.
      const extraction: Extraction = {
        capability: typeof json.capability === "string" ? json.capability : null,
        invocation_mode: (json.invocation_mode === "manual" || json.invocation_mode === "periodic") ? json.invocation_mode : null,
        triggers: Array.isArray(json.triggers) ? json.triggers : null,
        schedule: (json.schedule && typeof json.schedule === "object") ? json.schedule : null,
        output: typeof json.output === "string" ? json.output : null,
        missing: Array.isArray(json.missing) ? json.missing : [],
        confidence: (json.confidence && typeof json.confidence === "object") ? json.confidence : {},
      };
      return { ok: true, extraction };
    } catch (e) {
      clearTimeout(t);
      const isAbort = e instanceof Error && e.name === "AbortError";
      lastError = isAbort
        ? `request exceeded ${PER_REQUEST_TIMEOUT_MS}ms per-attempt timeout`
        : (e instanceof Error ? e.message : String(e));
      if (isAbort) break;
      await new Promise(res => setTimeout(res, 1500));
    }
  }
  return { ok: false, error: `Moonshot extraction failed after ${Math.round((Date.now() - start) / 1000)}s; last error: ${lastError}` };
}

// ──────────────────────────────────────────────────────────────────
// Draft persistence
// ──────────────────────────────────────────────────────────────────

function ensureDraftDir(): void {
  if (!existsSync(DRAFT_DIR)) mkdirSync(DRAFT_DIR, { recursive: true });
}

function saveDraft(draft: Draft): void {
  ensureDraftDir();
  draft.updated_at = new Date().toISOString();
  writeFileSync(join(DRAFT_DIR, `${draft.slug}.json`), JSON.stringify(draft, null, 2));
}

// ──────────────────────────────────────────────────────────────────
// Build draft from LLM extraction
// ──────────────────────────────────────────────────────────────────

function buildDraft(extraction: Extraction, sessionFile: string, messagesUsed: number): Draft {
  const provisionalSlug = `draft-${Date.now().toString(36)}`;
  const answers: CreateAnswers = {};
  const completed: number[] = [];

  // Q1 — capability
  if (extraction.capability && extraction.capability.trim().length > 0) {
    answers.capability = extraction.capability.trim();
    completed.push(1);
  }
  // Q2 — invocation_mode
  if (extraction.invocation_mode) {
    answers.invocation_mode = extraction.invocation_mode;
    completed.push(2);
  }
  // Q3 — triggers (manual) or schedule.frequency (periodic)
  if (extraction.invocation_mode === "manual" && extraction.triggers && extraction.triggers.length >= 2) {
    answers.triggers = extraction.triggers.map(t => String(t).trim()).filter(Boolean);
    completed.push(3);
  } else if (extraction.invocation_mode === "periodic" && extraction.schedule?.frequency) {
    answers.schedule = {
      frequency: extraction.schedule.frequency,
      timezone: extraction.schedule.timezone ?? "Asia/Kolkata",
      time_of_day: extraction.schedule.time_of_day,
      day_of_week: extraction.schedule.day_of_week,
      day_of_month: extraction.schedule.day_of_month,
      custom_cron: extraction.schedule.custom_cron,
    };
    completed.push(3);
  }
  // Q4 — schedule details (only for periodic with details required)
  if (extraction.invocation_mode === "periodic" && extraction.schedule?.frequency) {
    const freq = extraction.schedule.frequency;
    const hasDetail =
      (freq === "daily" && !!extraction.schedule.time_of_day) ||
      (freq === "weekly" && !!extraction.schedule.day_of_week && !!extraction.schedule.time_of_day) ||
      (freq === "monthly" && !!extraction.schedule.day_of_month && !!extraction.schedule.time_of_day) ||
      (freq === "hourly") ||
      (freq === "custom" && !!extraction.schedule.custom_cron);
    if (hasDetail) completed.push(4);
  }
  // Q5 — output
  if (extraction.output && extraction.output.trim().length > 0) {
    answers.output = extraction.output.trim();
    completed.push(5);
  }

  // For manual mode, Q4 (schedule details) is N/A — but elicit.ts's
  // nextCreateQuestion() only checks Q4 when periodic, so we don't need to
  // mark it complete. The completed_questions list looks like [1,2,3,5] for
  // a fully-elicited manual draft — that's the existing convention.

  const draft: Draft = {
    schema_version: 1,
    slug: provisionalSlug,
    mode: "create",
    sub_mode: "intake-from-context",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    completed_questions: completed,
    answers,
    intake_audit: {
      session_file: sessionFile,
      messages_used: messagesUsed,
      llm_extracted: answers,
      llm_missing: extraction.missing ?? [],
      llm_confidence: extraction.confidence ?? {},
    },
  };
  return draft;
}

// ──────────────────────────────────────────────────────────────────
// Resume into elicit (D2-ish: hand off to the existing question state
// machine so the SAME bullet summary / next-question UX renders)
// ──────────────────────────────────────────────────────────────────

function resumeViaElicit(slug: string): never {
  const elicitPath = join(REPO_ROOT, "meta", "skill-author", "scripts", "elicit.ts");
  const proc = Bun.spawnSync({
    cmd: [BUN_BIN, "run", elicitPath, "--mode=resume", `--slug=${slug}`],
    env: { ...process.env, REPO_ROOT },
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = proc.stdout.toString().trim();
  if (!stdout) {
    emit({
      ok: false,
      phase: "error",
      user_message: "Couldn't resume the draft. Try `/teach` to start fresh.",
      draft_slug: slug,
      detail: { elicit_stderr: proc.stderr.toString().slice(0, 300) },
    });
  }
  console.log(stdout);
  process.exit(0);
}

// ──────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);
  const mode = (args["mode"] as string) ?? "create-oneshot";
  if (mode !== "create-oneshot") {
    emit({ ok: false, phase: "error", user_message: `intake-from-context: unknown mode \`${mode}\``, draft_slug: null });
  }

  const latestMessage = (args["message"] as string ?? "").trim();
  if (!latestMessage) {
    fallbackToFreshElicit("no message provided");
  }

  // Resolve session file: explicit arg > auto-detect freshest jsonl. If we
  // can't find one, that's OK — extraction can work off the --message alone
  // (added 2026-05-22 after image-v33 bug report: the user's FIRST detailed
  // message has enough spec to extract from without any prior chat).
  let sessionFile = (args["session-file"] as string) ?? "";
  if (!sessionFile) sessionFile = findActiveSessionFile() ?? "";

  let messages: ChatMessage[] = [];
  if (sessionFile && existsSync(sessionFile)) {
    messages = readSessionTail(sessionFile, RECENT_MESSAGES);
  }

  // Heuristic — is the user's CURRENT message substantive enough to extract
  // from on its own? We require at least 80 chars or 12 words, since shorter
  // messages like "yes implement this" are confirmations, not specs.
  const wordCount = latestMessage.split(/\s+/).filter(Boolean).length;
  const messageIsSubstantive = latestMessage.length >= 80 || wordCount >= 12;

  // Three cases:
  //   (a) Substantive first message, no useful chat history → extract from
  //       message alone. This is the image-v33 case.
  //   (b) Short "yes implement this" confirmation, chat has history →
  //       extract from chat history (PR #121 original use case).
  //   (c) Short message, no chat → no useful input, fall back to elicit Q1.
  const haveContext = messages.length >= 2 || messageIsSubstantive;
  if (!haveContext) {
    fallbackToFreshElicit(`no chat history and message is too short (${latestMessage.length} chars, ${wordCount} words)`);
  }

  const llmResult = await callKimiForExtraction(messages, latestMessage);
  if (!llmResult.ok || !llmResult.extraction) {
    fallbackToFreshElicit(`LLM extraction failed: ${llmResult.error ?? "unknown"}`);
  }

  const draft = buildDraft(llmResult.extraction!, sessionFile, messages.length);

  // If we filled nothing at all, behave like fresh elicit.
  if (draft.completed_questions.length === 0) {
    fallbackToFreshElicit("LLM extracted nothing useful");
  }

  saveDraft(draft);

  // Hand off to elicit's resume mode. If completed=[1..5] (or [1,2,3,5] for
  // manual), it'll set awaiting_confirmation and emit the bullet summary.
  // Otherwise it'll ask the next missing question.
  resumeViaElicit(draft.slug);
}

main().catch((err: unknown) => {
  emit({
    ok: false,
    phase: "error",
    user_message: "Hit an error setting up the new skill. Try `/teach` to start fresh.",
    draft_slug: null,
    detail: { error: err instanceof Error ? err.message : String(err) },
  });
});
