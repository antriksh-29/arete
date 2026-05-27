#!/usr/bin/env -S bun run
/**
 * elicit.ts — 4Q (create) / 3Q (refine) state machine for skill-author.
 *
 * Invoked per Telegram turn. Reads the current user message + any prior
 * draft from .context/skill-author-drafts/<slug>.json, decides which
 * question is next, emits a JSON envelope with `user_message` for
 * Arete to relay verbatim (Rule 0.7).
 *
 * Modes:
 *   --mode=create                          /teach or INTENT=skill_create
 *   --mode=refine --target=<name>          /refine <name> or INTENT=skill_refine TARGET=<name>
 *   --mode=resume --slug=<draft-slug>      user picked "continue draft" on a resume prompt
 *   --mode=ingest-answer --slug=<slug>     a user turn answering question N (most common)
 *
 * Input options:
 *   --message=<text>                       the user's verbatim message
 *   --message-stdin                        read message from stdin
 *
 * Output: single-line JSON envelope to stdout. See meta/skill-author/SKILL.md
 * → "Output format" for the schema.
 *
 * This script has NO LLM call. Pure deterministic state machine. The LLM
 * lives in safety-scan.ts (next phase) and in the inline intent router
 * (one turn before). Total latency target: <50ms per call.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync } from "fs";
import { join, resolve } from "path";

// ──────────────────────────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────────────────────────

type Mode = "create" | "refine" | "resume" | "ingest-answer";

interface CreateAnswers {
  capability?: string;                     // Q1
  invocation_mode?: "manual" | "periodic"; // Q2
  triggers?: string[];                     // Q2a (only when invocation_mode=manual)
  schedule?: {                             // Q2b (only when invocation_mode=periodic)
    frequency: "hourly" | "daily" | "weekly" | "monthly" | "custom";
    time_of_day?: string;                  // "08:00" for daily/weekly/monthly
    day_of_week?: string;                  // "monday" for weekly
    day_of_month?: number;                 // 1..28 for monthly
    custom_cron?: string;                  // raw cron expression for custom
    timezone?: string;                     // default "Asia/Kolkata"
  };
  output?: string;                         // Q3
}

interface RefineAnswers {
  target: string;                  // resolved <tier>/<name>
  change_statement?: string;       // R1
  desired_behavior?: string;       // R2
  additional_triggers?: string[];  // R3 (possibly empty)
}

interface Draft {
  schema_version: 1;
  slug: string;
  mode: "create" | "refine";
  created_at: string;
  updated_at: string;
  completed_questions: number[];   // [1,2,3,5] for fully-elicited manual create
  answers: CreateAnswers | RefineAnswers;
  // True once all questions have been answered and the draft summary
  // was shown. The next user message is expected to be a confirmation
  // (yes/no/cancel). See ingestAnswer for handling.
  awaiting_confirmation?: boolean;
  // populated only for refine:
  source_tier?: "skills" | "openclaw/skills" | "user-skills";
  source_sha?: string;             // captured at /refine time
  source_triggers?: string[];      // verbatim from source eval.yaml
}

interface Envelope {
  ok: boolean;
  phase: "elicit" | "safety" | "routing" | "commit" | "deploy" | "done" | "error" | "rate_limit";
  user_message: string;            // relayed to Telegram byte-for-byte
  draft_slug: string | null;
  verdict?: string;
  detail?: Record<string, unknown>;
}

// ──────────────────────────────────────────────────────────────────
// Paths
// ──────────────────────────────────────────────────────────────────

const REPO_ROOT = process.env.REPO_ROOT ?? resolve(__dirname, "../../..");
const DRAFT_DIR = join(REPO_ROOT, ".context", "skill-author-drafts");
const SKILLS_DIRS = ["skills", "openclaw/skills", "user-skills"] as const;

// ──────────────────────────────────────────────────────────────────
// CLI parsing
// ──────────────────────────────────────────────────────────────────

function parseArgs(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const arg of argv.slice(2)) {
    if (!arg.startsWith("--")) continue;
    const eq = arg.indexOf("=");
    if (eq === -1) {
      out[arg.slice(2)] = true;
    } else {
      out[arg.slice(2, eq)] = arg.slice(eq + 1);
    }
  }
  return out;
}

// ──────────────────────────────────────────────────────────────────
// Slug derivation
// ──────────────────────────────────────────────────────────────────

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .slice(0, 40)
    .replace(/^-+|-+$/g, "");
}

// ──────────────────────────────────────────────────────────────────
// Source-tier resolution (for refine mode)
// ──────────────────────────────────────────────────────────────────

function resolveSourceTier(name: string): { tier: typeof SKILLS_DIRS[number]; path: string } | null {
  for (const tier of SKILLS_DIRS) {
    const p = join(REPO_ROOT, tier, name, "SKILL.md");
    if (existsSync(p)) return { tier, path: join(REPO_ROOT, tier, name) };
  }
  return null;
}

function readSourceTriggers(skillDir: string): string[] {
  const evalPath = join(skillDir, "eval.yaml");
  if (!existsSync(evalPath)) return [];
  // Lightweight extraction — we don't need full YAML, just the utterance: lines.
  const text = readFileSync(evalPath, "utf8");
  const triggers: string[] = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*-?\s*utterance:\s*"?([^"\n]+?)"?\s*$/);
    if (m) triggers.push(m[1]);
  }
  return triggers;
}

function readSourceSha(skillDir: string): string {
  // git rev-parse HEAD: short SHA at the time of fork.
  try {
    const child = Bun.spawnSync(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"]);
    return child.stdout.toString().trim() || "unknown";
  } catch {
    return "unknown";
  }
}

// ──────────────────────────────────────────────────────────────────
// Draft persistence
// ──────────────────────────────────────────────────────────────────

function ensureDraftDir(): void {
  if (!existsSync(DRAFT_DIR)) mkdirSync(DRAFT_DIR, { recursive: true });
}

function draftPath(slug: string): string {
  return join(DRAFT_DIR, `${slug}.json`);
}

function loadDraft(slug: string): Draft | null {
  const p = draftPath(slug);
  if (!existsSync(p)) return null;
  try {
    return JSON.parse(readFileSync(p, "utf8")) as Draft;
  } catch {
    return null;
  }
}

function saveDraft(draft: Draft): void {
  ensureDraftDir();
  draft.updated_at = new Date().toISOString();
  writeFileSync(draftPath(draft.slug), JSON.stringify(draft, null, 2));
}

function listPendingDrafts(): Draft[] {
  if (!existsSync(DRAFT_DIR)) return [];
  const out: Draft[] = [];
  for (const f of readdirSync(DRAFT_DIR)) {
    if (!f.endsWith(".json")) continue;
    try {
      const d = JSON.parse(readFileSync(join(DRAFT_DIR, f), "utf8")) as Draft;
      // "pending" means not all questions answered
      const required = d.mode === "create" ? 4 : 3;
      if (d.completed_questions.length < required) out.push(d);
    } catch { /* skip malformed */ }
  }
  return out;
}

// ──────────────────────────────────────────────────────────────────
// Question text (mirrors meta/skill-author/prompts/elicit.md)
// ──────────────────────────────────────────────────────────────────

// CREATE questions. Q2 has a branch — depending on `invocation_mode`,
// we ask either Q2a (triggers, for manual) or Q2b (schedule, for periodic).
//
// Friend-voice wording (PR #142, post-2026-05-24 retune). Previously
// these strings were duplicated in meta/skill-author/prompts/elicit.md
// (the prompt source) and never propagated here — Bug F regression.
// The .md file is now the canonical reference for the wording.
const Q_CREATE_BASE: Record<number, string> = {
  1: "nice, let's set this up. one line: what should I do for you?\n\n(like \"send me a motivational quote every morning\", or \"when I forward a receipt photo, log the amount, merchant, and date to my expenses\".)",
  2: "should this run when you ask, or on a schedule?\n\n(say \"when I ask\" or \"on a schedule\".)",
  3: "where should the result land? back here in chat, or somewhere else (file, note, email, calendar, etc)?",
};

// Q2a — when invocation_mode=manual
const Q_CREATE_2A_MANUAL =
  "cool. what 2-3 phrases should I listen for? (more specific is better, so it doesn't collide with anything else I do.)\n\n(like \"morning quote\", \"give me my morning quote\", \"kick me off with a quote\".)";

// Q2b — when invocation_mode=periodic
const Q_CREATE_2B_PERIODIC =
  "how often? (say hourly, daily, weekly, or monthly, or give me a specific rhythm if it's something else.)";

// Follow-up after Q2b: schedule details. Friend-voice wording, no
// cron expressions in user-visible text.
function q2bScheduleDetails(freq: string): string {
  switch (freq) {
    case "daily":   return "what time? (24h, IST. like `08:00` for 8am or `22:30` for 10:30pm.)";
    case "weekly":  return "which day and time? (like `monday 08:00` or `saturday 19:00`.)";
    case "monthly": return "which day of the month, and what time? (like `1 09:00` for 9am on the 1st.)";
    case "hourly":  return "on the hour, or some other minute past? (say `on the hour` or `:15`, etc.)";
    case "custom":  return "paste the timing you want. plain English is fine, I'll figure it out, like 'every 15 min between 8am and 10pm'.";
    default:        return "";
  }
}

const Q_REFINE: Record<number, (ctx: { target: string; existingTriggers: string[] }) => string> = {
  // Note: `target` is the slug, but we don't surface it in chat per
  // identity/SOUL.md hard blocklist. Refer to the skill as "this one"
  // and rely on the user remembering which skill they just asked to
  // tweak (the prior message-id linkage handles disambiguation).
  1: () => `on it. what's not working right now, or what should it do that it doesn't?`,
  2: () => "got it. when it runs, what should happen instead?",
  3: ({ existingTriggers }) => {
    const list = existingTriggers.length ? existingTriggers.map(t => `"${t}"`).join(", ") : "(none)";
    return `keep the existing trigger phrases, or want to add new ones?\n\n(right now I'm listening for: ${list}.)`;
  },
};

// ──────────────────────────────────────────────────────────────────
// Answer parsing
// ──────────────────────────────────────────────────────────────────

function parseTriggers(text: string): string[] {
  // Accept comma-separated, quoted, or newline-separated. Strip empty.
  // "good morning quote", "give me my morning quote"  →  ["good morning quote", ...]
  const quoted = [...text.matchAll(/"([^"]+)"/g)].map(m => m[1].trim());
  if (quoted.length >= 2) return quoted;
  // Comma split
  if (text.includes(",")) {
    return text.split(",").map(s => s.trim().replace(/^["']|["']$/g, "")).filter(s => s.length > 0);
  }
  // Newline split
  return text.split("\n").map(s => s.trim()).filter(s => s.length > 0);
}

function parseYesNo(text: string): boolean | null {
  const t = text.trim().toLowerCase();
  if (/^(y|yes|yeah|yep|sure|ok|please|with tests?|add tests?)\b/.test(t)) return true;
  if (/^(n|no|nope|skip|one[- ]?shot|no thanks?)\b/.test(t)) return false;
  return null;
}

// ──────────────────────────────────────────────────────────────────
// Phase logic
// ──────────────────────────────────────────────────────────────────

function envelope(out: Envelope): never {
  console.log(JSON.stringify(out));
  process.exit(0);
}

// Question slot meanings for CREATE:
//   1 → capability
//   2 → invocation_mode (manual | periodic)
//   3 → triggers (if mode=manual) OR schedule frequency (if mode=periodic)
//   4 → schedule details (only when mode=periodic and freq needs them)
//   5 → output
//
// For manual mode the sequence is [1,2,3,5] (4 questions visible).
// For periodic mode it's [1,2,3,4,5] (5 questions, but Q3+Q4 are tightly
// related — both about scheduling).
function nextCreateQuestion(draft: Draft): number | null {
  const a = draft.answers as CreateAnswers;
  const done = new Set(draft.completed_questions);
  if (!done.has(1)) return 1;
  if (!done.has(2)) return 2;
  if (!done.has(3)) return 3;
  // Q4 is only needed when periodic and the freq needs details.
  if (a.invocation_mode === "periodic" && !done.has(4)) {
    const freq = a.schedule?.frequency;
    if (freq && freq !== "custom" && q2bScheduleDetails(freq)) return 4;
    // "hourly on the hour" or "custom" answered at Q3 → skip Q4
  }
  if (!done.has(5)) return 5;
  return null;
}

function nextRefineQuestion(draft: Draft): number | null {
  for (let i = 1; i <= 3; i++) {
    if (!draft.completed_questions.includes(i)) return i;
  }
  return null;
}

function buildDraftSummary(draft: Draft): string {
  // Friend-voice (Bug L fix, 2026-05-25). PR #139 + #141 updated
  // elicit.md and the Q_CREATE/Q_REFINE question tables, but the
  // confirmFooter + the four summary templates below were missed —
  // they still emitted the old engineer-voice menu with **bold**
  // markdown that renders as literal asterisks in text-mode Telegram.
  // Now: friend-voice footer, no markdown, no slug exposure, no
  // workspace-tier references.
  const confirmFooter = "\n\nshall I save this, or want to tweak?";
  // Refine-diff drafts carry their own pre-formatted bullet summary from
  // the LLM (change_summary array). Re-emit it verbatim on resume so the
  // user sees the same bullets they saw the first time.
  if ((draft as any).sub_mode === "refine-diff") {
    const summary = ((draft as any).change_summary ?? []) as string[];
    const bulletsText = summary.map(s => `• ${s}`).join("\n");
    const target = (draft.answers as any).target ?? draft.slug;
    const friendlyTarget = String(target).replace(/-/g, " ");
    return `here's the change I'd make to your ${friendlyTarget}:\n\n${bulletsText}${confirmFooter}`;
  }
  if (draft.mode === "create") {
    const a = draft.answers as CreateAnswers;
    if (a.invocation_mode === "manual") {
      const triggers = (a.triggers ?? []).map(t => `"${t}"`).join(", ");
      return `got it. when you say things like ${triggers}, I'll ${a.capability}, and reply with ${a.output}.${confirmFooter}`;
    } else {
      // periodic
      const sched = a.schedule!;
      let when = "";
      if (sched.frequency === "daily") when = `every day at ${sched.time_of_day} IST`;
      else if (sched.frequency === "weekly") when = `every ${sched.day_of_week} at ${sched.time_of_day} IST`;
      else if (sched.frequency === "monthly") when = `on the ${sched.day_of_month}${ordinalSuffix(sched.day_of_month!)} of every month at ${sched.time_of_day} IST`;
      else if (sched.frequency === "hourly") when = `every hour at ${sched.time_of_day}`;
      else if (sched.frequency === "custom") when = `on the schedule you gave`;
      return `got it. ${when}, I'll ${a.capability}, and reply with ${a.output}.${confirmFooter}`;
    }
  } else {
    const a = draft.answers as RefineAnswers;
    const friendlyTarget = String(a.target).replace(/-/g, " ");
    const newTriggersLine = (a.additional_triggers && a.additional_triggers.length > 0)
      ? `\n\nalso listening for: ${a.additional_triggers.map(t => `"${t}"`).join(", ")}`
      : "";
    return `got it, tweaking your ${friendlyTarget}.\n\nbefore: ${a.change_statement}\nafter: ${a.desired_behavior}${newTriggersLine}${confirmFooter}`;
  }
}

// ──────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);
  const mode = (args["mode"] as Mode) ?? "create";

  // ── Read user message (if any) ────────────────────────────────
  let userMessage = "";
  if (args["message-stdin"]) {
    userMessage = (await Bun.stdin.text()).trim();
  } else if (typeof args["message"] === "string") {
    userMessage = (args["message"] as string).trim();
  }

  // ── Mode: resume (user picked "continue") ─────────────────────
  if (mode === "resume") {
    const slug = args["slug"] as string;
    const draft = slug ? loadDraft(slug) : null;
    if (!draft) {
      envelope({ ok: false, phase: "error", user_message: "Couldn't find that draft. Want to start fresh?", draft_slug: null });
    }
    return promptNextQuestion(draft!);
  }

  // ── Mode: ingest-answer (most common path) ────────────────────
  if (mode === "ingest-answer") {
    const slug = args["slug"] as string;
    const draft = slug ? loadDraft(slug) : null;
    if (!draft) {
      envelope({ ok: false, phase: "error", user_message: "I lost track of that draft. Let's start over — say `/teach` or describe what you want.", draft_slug: null });
    }
    return ingestAnswer(draft!, userMessage);
  }

  // ── Mode: create ──────────────────────────────────────────────
  if (mode === "create") {
    // Check for an in-flight draft — if one exists, offer to resume.
    const pending = listPendingDrafts().filter(d => d.mode === "create");
    if (pending.length > 0 && !args["force-new"]) {
      const d = pending[0];
      const a = d.answers as CreateAnswers;
      const summary = a.capability ?? "(no capability set yet)";
      const relative = relativeTime(d.updated_at);
      envelope({
        ok: true,
        phase: "elicit",
        user_message: `You have an unfinished draft from ${relative}:\n"${summary}"\n\nContinue where we left off, or start fresh?`,
        draft_slug: d.slug,
        detail: { sub_phase: "resume_prompt", existing_slug: d.slug },
      });
    }
    // Start a new draft. The slug is provisional; finalized at commit time.
    const provisionalSlug = `draft-${Date.now().toString(36)}`;
    const draft: Draft = {
      schema_version: 1,
      slug: provisionalSlug,
      mode: "create",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      completed_questions: [],
      answers: {},
    };
    saveDraft(draft);
    return promptNextQuestion(draft);
  }

  // ── Mode: refine ──────────────────────────────────────────────
  if (mode === "refine") {
    const target = args["target"] as string;
    if (!target) {
      envelope({ ok: false, phase: "error", user_message: "Which skill should I refine? Try `/refine <name>` or describe what to change.", draft_slug: null });
    }
    // Don't clobber a refine-diff draft that's awaiting user confirmation.
    // If the agent accidentally re-runs `--mode=refine` while a one-shot
    // proposal is sitting at the yes/no/cancel gate, we'd lose the LLM's
    // proposed content and force the user back to Q&A. Observed
    // 2026-05-21: ship-path test failed exactly this way — the agent
    // looped and called --mode=refine, overwriting the awaiting-confirm
    // draft. Treat any such collision as a resume signal and re-emit
    // the original draft summary instead.
    const existing = loadDraft(target);
    if (existing && (existing as any).sub_mode === "refine-diff" && existing.awaiting_confirmation === true) {
      envelope({
        ok: true,
        phase: "elicit",
        user_message: buildDraftSummary(existing),
        draft_slug: existing.slug,
        detail: { sub_phase: "draft_summary", awaiting_confirmation: true, sub_mode: "refine-diff", resumed_existing: true },
      });
    }
    const src = resolveSourceTier(target);
    if (!src) {
      envelope({ ok: false, phase: "error", user_message: `I don't see a skill called \`${target}\`. Available skills are under skills/, openclaw/skills/, or user-skills/.`, draft_slug: null });
    }
    const existingTriggers = readSourceTriggers(src!.path);
    const sourceSha = src!.tier === "user-skills" ? "current" : readSourceSha(src!.path);

    const draft: Draft = {
      schema_version: 1,
      slug: target,
      mode: "refine",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      completed_questions: [],
      answers: { target, additional_triggers: [] } as RefineAnswers,
      source_tier: src!.tier,
      source_sha: sourceSha,
      source_triggers: existingTriggers,
    };
    saveDraft(draft);
    return promptNextQuestion(draft);
  }

  envelope({ ok: false, phase: "error", user_message: `Unknown mode: ${mode}`, draft_slug: null });
}

// ──────────────────────────────────────────────────────────────────
// Question dispatch + answer ingestion
// ──────────────────────────────────────────────────────────────────

function promptNextQuestion(draft: Draft): never {
  const next = draft.mode === "create" ? nextCreateQuestion(draft) : nextRefineQuestion(draft);
  if (next === null) {
    // All questions answered — flag the draft as awaiting user confirmation.
    // The next ingestAnswer call will interpret the message as yes/no/cancel
    // instead of as Q-N's answer. Persist so subsequent turns see the state.
    draft.awaiting_confirmation = true;
    saveDraft(draft);
    envelope({
      ok: true,
      phase: "elicit",
      user_message: buildDraftSummary(draft),
      draft_slug: draft.slug,
      detail: { sub_phase: "draft_summary", awaiting_confirmation: true },
    });
  }
  let text: string;
  if (draft.mode === "create") {
    const a = draft.answers as CreateAnswers;
    if (next === 1) text = Q_CREATE_BASE[1];
    else if (next === 2) text = Q_CREATE_BASE[2];
    else if (next === 3) {
      text = a.invocation_mode === "periodic" ? Q_CREATE_2B_PERIODIC : Q_CREATE_2A_MANUAL;
    }
    else if (next === 4) {
      text = q2bScheduleDetails(a.schedule?.frequency ?? "");
    }
    else { // next === 5
      text = Q_CREATE_BASE[3];
    }
  } else {
    text = Q_REFINE[next]({
      target: (draft.answers as RefineAnswers).target,
      existingTriggers: draft.source_triggers ?? [],
    });
  }
  envelope({
    ok: true,
    phase: "elicit",
    user_message: text,
    draft_slug: draft.slug,
    detail: { sub_phase: "question", question_number: next },
  });
}

// ──────────────────────────────────────────────────────────────────
// Confirmation phase (yes/no/cancel after the draft summary)
// ──────────────────────────────────────────────────────────────────

type ConfirmAnswer = "yes" | "no" | "cancel" | "ambiguous";

function parseConfirmation(text: string): ConfirmAnswer {
  const t = text.trim().toLowerCase();
  // Generous yes-matching — user might type any positive phrase.
  if (/^(yes|y|yep|yeah|ya|sure|ok|okay|save it|save|ship it|do it|go|go ahead|sounds good|sounds great|sounds right|looks good|looks right|perfect|that works|awesome|nice|right|correct|👍|✅)\b/.test(t)) return "yes";
  if (/^(no|n|nope|nah|not quite|wrong|not right|incorrect|tweak|edit|change|fix)\b/.test(t)) return "no";
  if (/^(cancel|abort|stop|nevermind|never mind|forget it|discard|throw it away|drop it)\b/.test(t)) return "cancel";
  return "ambiguous";
}

function handleConfirmation(draft: Draft, message: string): never {
  const c = parseConfirmation(message);
  if (c === "ambiguous") {
    // Bug N fix (2026-05-26): track consecutive ambiguous responses and
    // auto-drop the draft after 2. Previously this state was a permanent
    // trap — user types anything off-script (a new ask, a follow-up
    // question, a typo) and gets bounced back with the same prompt
    // forever. Auto-drop after 2 ambiguous turns frees the conversation.
    const newCount = ((draft as any).ambiguous_count ?? 0) + 1;
    if (newCount >= 2) {
      try {
        const p = draftPath(draft.slug);
        if (existsSync(p)) require("fs").unlinkSync(p);
      } catch { /* best-effort */ }
      envelope({
        ok: true,
        phase: "done",
        user_message: "ok, dropping that. what's up?",
        draft_slug: draft.slug,
        detail: { sub_phase: "ambiguous_expired" },
      });
    }
    (draft as any).ambiguous_count = newCount;
    saveDraft(draft);
    envelope({
      ok: true,
      phase: "elicit",
      user_message: `save it, or drop it?`,
      draft_slug: draft.slug,
      detail: { sub_phase: "confirmation_retry", ambiguous_count: newCount },
    });
  }
  if (c === "cancel") {
    try {
      const p = draftPath(draft.slug);
      if (existsSync(p)) require("fs").unlinkSync(p);
    } catch { /* best-effort */ }
    envelope({
      ok: true,
      phase: "done",
      user_message: "dropped it.",
      draft_slug: draft.slug,
      detail: { sub_phase: "cancelled" },
    });
  }
  if (c === "no") {
    // v1: don't loop back to specific questions. Delete the draft and
    // free the conversation. Friend-voice: no slug, no /teach pointer.
    try {
      const p = draftPath(draft.slug);
      if (existsSync(p)) require("fs").unlinkSync(p);
    } catch { /* best-effort */ }
    envelope({
      ok: true,
      phase: "done",
      user_message: "ok, dropped it. want to try again?",
      draft_slug: draft.slug,
      detail: { sub_phase: "rejected_restart" },
    });
  }
  // c === "yes" — chain to commit-and-deploy synchronously.
  return runCommitChain(draft);
}

function runCommitChain(draft: Draft): never {
  // Spawn commit-and-deploy.ts as a subprocess. It does materialize +
  // git commit + push (which triggers GH Actions server-side safety
  // scan + deploy). Returns its envelope verbatim — that's what the
  // user sees ("Creating this new skill for you. Please wait 1–2 min.").
  //
  // Why synchronous (no parallel safety-scan/routing-eval in elicit):
  // (1) commit-and-deploy.ts materializes the SKILL.md + eval.yaml; we'd
  //     need those files on disk before safety-scan can scan them.
  // (2) GH Actions runs the server-side regex safety check + file
  //     allowlist on every push (deploy.yml T8 gate). That's defense
  //     in depth.
  // (3) Routing-eval needs the materialized eval.yaml too. Same chain.
  // For v1.1 we ship the minimum that unblocks the user; v1.2 can
  // refactor to run safety+routing pre-commit if false positives bite.
  const draftFile = draftPath(draft.slug);

  // PR 2: pre-commit LLM polish for create drafts. generate-skill.ts
  // mutates the draft on disk with proposed_skill_md_body +
  // proposed_eval_yaml; commit-and-deploy.ts picks those up. On any
  // failure (LLM error, validation reject, timeout), generate-skill
  // exits 0 silently and commit-and-deploy uses the existing
  // boilerplate generator. No-worse-than-today guarantee.
  if (draft.mode === "create") {
    try {
      const generatePath = join(REPO_ROOT, "meta", "skill-author", "scripts", "generate-skill.ts");
      const bunBin = process.env.BUN_INSTALL
        ? join(process.env.BUN_INSTALL, "bin", "bun")
        : join(process.env.HOME ?? "", ".bun", "bin", "bun");
      Bun.spawnSync({
        cmd: [bunBin, "run", generatePath, `--draft-file=${draftFile}`],
        env: { ...process.env, REPO_ROOT },
        stdout: "pipe",
        stderr: "pipe",
      });
      // Don't check exit code or stdout — generate-skill is best-effort.
      // If it succeeded, commit-and-deploy will see the new fields on
      // disk. If it failed, it left the draft untouched.
    } catch {
      // best-effort
    }
  }

  const scriptPath = join(REPO_ROOT, "meta", "skill-author", "scripts", "commit-and-deploy.ts");
  try {
    const bunBin2 = process.env.BUN_INSTALL
      ? join(process.env.BUN_INSTALL, "bin", "bun")
      : join(process.env.HOME ?? "", ".bun", "bin", "bun");
    const proc = Bun.spawnSync({
      cmd: [bunBin2, "run", scriptPath, `--draft-file=${draftFile}`],
      env: { ...process.env, REPO_ROOT },
      stdout: "pipe",
      stderr: "pipe",
    });
    const stdout = proc.stdout.toString().trim();
    const stderr = proc.stderr.toString().trim();
    if (!stdout) {
      envelope({
        ok: false,
        phase: "error",
        user_message: "Something went wrong on the commit step — I couldn't finish creating the skill. Try `/teach` again in a minute.",
        draft_slug: draft.slug,
        detail: { commit_stderr: stderr.slice(0, 500), commit_exit: proc.exitCode },
      });
    }
    // Pass through the commit-and-deploy envelope. Its user_message is
    // typically "📦 Committed <slug> @ <sha> and pushed. Server-side
    // gates + deploy are running now…" which is exactly the UX we want.
    const passthrough = JSON.parse(stdout);
    // Delete the draft once committed — its job is done.
    if (passthrough.ok && passthrough.verdict === "deploy_ok") {
      try { require("fs").unlinkSync(draftFile); } catch { /* best-effort */ }
    }
    console.log(stdout);
    process.exit(passthrough.ok ? 0 : 1);
  } catch (err) {
    envelope({
      ok: false,
      phase: "error",
      user_message: "Hit a snag spawning the commit step. Try `/teach` again in a minute.",
      draft_slug: draft.slug,
      detail: { error: err instanceof Error ? err.message : String(err) },
    });
  }
}

function ingestAnswer(draft: Draft, message: string): never {
  if (!message) {
    envelope({ ok: false, phase: "error", user_message: "I didn't catch that — could you re-send your answer?", draft_slug: draft.slug });
  }
  // ── CONFIRMATION PHASE (added 2026-05-19) ─────────────────────────
  // If the draft was already flagged as awaiting confirmation (every
  // question answered + summary shown), the current message is a
  // yes/no/cancel reply, NOT an answer to a question. Route accordingly.
  if (draft.awaiting_confirmation) {
    return handleConfirmation(draft, message);
  }
  const next = draft.mode === "create" ? nextCreateQuestion(draft) : nextRefineQuestion(draft);
  if (next === null) {
    // Defensive: nextCreateQuestion returned null but awaiting_confirmation
    // was not set. Treat as if the user just got the summary — set the
    // flag and re-route. Should only happen on drafts created pre-fix.
    draft.awaiting_confirmation = true;
    saveDraft(draft);
    return handleConfirmation(draft, message);
  }

  if (draft.mode === "create") {
    const a = draft.answers as CreateAnswers;
    switch (next) {
      case 1: a.capability = message; break;
      case 2: {
        const m = message.trim().toLowerCase();
        let mode: "manual" | "periodic" | null = null;
        if (/^(manual|when i ask|on demand|trigger|just when)/.test(m)) mode = "manual";
        else if (/^(periodic|schedule|cron|automat|every|recurring|daily|hourly|weekly|monthly)/.test(m)) mode = "periodic";
        if (!mode) {
          envelope({
            ok: true,
            phase: "elicit",
            user_message: "Either \"manual\" (you'll trigger it with words) or \"periodic\" (runs on a schedule). Which one?",
            draft_slug: draft.slug,
            detail: { sub_phase: "question", question_number: 2, retry: true },
          });
        }
        a.invocation_mode = mode!;
        break;
      }
      case 3: {
        if (a.invocation_mode === "manual") {
          const triggers = parseTriggers(message);
          if (triggers.length < 2) {
            envelope({
              ok: true,
              phase: "elicit",
              user_message: `Only caught ${triggers.length} trigger phrase${triggers.length === 1 ? "" : "s"}. Give me at least 2 — separate them with commas or put each in quotes. (Single-phrase triggers fire too often by accident.)`,
              draft_slug: draft.slug,
              detail: { sub_phase: "question", question_number: 3, retry: true },
            });
          }
          a.triggers = triggers;
        } else {
          // periodic — parse the frequency
          const m = message.trim().toLowerCase();
          const sched: any = { timezone: "Asia/Kolkata" };
          if (/^hourly/.test(m)) sched.frequency = "hourly";
          else if (/^daily/.test(m)) sched.frequency = "daily";
          else if (/^weekly/.test(m)) sched.frequency = "weekly";
          else if (/^monthly/.test(m)) sched.frequency = "monthly";
          else if (/^custom/.test(m)) sched.frequency = "custom";
          else {
            envelope({
              ok: true,
              phase: "elicit",
              user_message: "One of: `hourly` / `daily` / `weekly` / `monthly` / `custom`. Which?",
              draft_slug: draft.slug,
              detail: { sub_phase: "question", question_number: 3, retry: true },
            });
          }
          a.schedule = sched;
        }
        break;
      }
      case 4: {
        // schedule details (only when periodic)
        const sched = a.schedule!;
        const m = message.trim().toLowerCase();
        if (sched.frequency === "daily") {
          const tm = m.match(/(\d{1,2}):?(\d{0,2})\s*(am|pm)?/);
          if (!tm) {
            envelope({
              ok: true, phase: "elicit",
              user_message: "Couldn't parse the time. Try 24h format like `08:00` or `22:30`.",
              draft_slug: draft.slug,
              detail: { sub_phase: "question", question_number: 4, retry: true },
            });
          }
          let hr = parseInt(tm![1], 10);
          const mn = tm![2] ? parseInt(tm![2], 10) : 0;
          if (tm![3] === "pm" && hr < 12) hr += 12;
          if (tm![3] === "am" && hr === 12) hr = 0;
          sched.time_of_day = `${String(hr).padStart(2, "0")}:${String(mn).padStart(2, "0")}`;
        } else if (sched.frequency === "weekly") {
          const dm = m.match(/(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(\d{1,2}):?(\d{0,2})/);
          if (!dm) {
            envelope({
              ok: true, phase: "elicit",
              user_message: "Format: `<day> <time>`. Example: `monday 08:00`. Which day + time?",
              draft_slug: draft.slug,
              detail: { sub_phase: "question", question_number: 4, retry: true },
            });
          }
          sched.day_of_week = dm![1];
          let hr = parseInt(dm![2], 10);
          const mn = dm![3] ? parseInt(dm![3], 10) : 0;
          sched.time_of_day = `${String(hr).padStart(2, "0")}:${String(mn).padStart(2, "0")}`;
        } else if (sched.frequency === "monthly") {
          const mm = m.match(/(\d{1,2})\s+(\d{1,2}):?(\d{0,2})/);
          if (!mm) {
            envelope({
              ok: true, phase: "elicit",
              user_message: "Format: `<day-of-month> <time>`. Example: `1 09:00` for 9am on the 1st.",
              draft_slug: draft.slug,
              detail: { sub_phase: "question", question_number: 4, retry: true },
            });
          }
          sched.day_of_month = parseInt(mm![1], 10);
          const hr = parseInt(mm![2], 10);
          const mn = mm![3] ? parseInt(mm![3], 10) : 0;
          sched.time_of_day = `${String(hr).padStart(2, "0")}:${String(mn).padStart(2, "0")}`;
        } else if (sched.frequency === "hourly") {
          const om = m.match(/:?(\d{1,2})/);
          sched.time_of_day = om ? `:${String(parseInt(om[1], 10)).padStart(2, "0")}` : ":00";
        } else if (sched.frequency === "custom") {
          // 5-field cron sanity check
          const parts = m.split(/\s+/);
          if (parts.length !== 5) {
            envelope({
              ok: true, phase: "elicit",
              user_message: "Cron format is 5 space-separated fields: `minute hour dom month dow`. Try again.",
              draft_slug: draft.slug,
              detail: { sub_phase: "question", question_number: 4, retry: true },
            });
          }
          sched.custom_cron = m;
        }
        break;
      }
      case 5: a.output = message; break;
    }
    // Only mark Q3 complete when its inputs validated
    if (next === 3) {
      if (a.invocation_mode === "manual" && (a.triggers?.length ?? 0) < 2) {
        // retry path already enveloped above
      } else if (a.invocation_mode === "periodic" && !a.schedule?.frequency) {
        // retry path already enveloped above
      } else {
        draft.completed_questions.push(3);
      }
    } else {
      draft.completed_questions.push(next);
    }
  } else {
    const a = draft.answers as RefineAnswers;
    switch (next) {
      case 1: a.change_statement = message; break;
      case 2: a.desired_behavior = message; break;
      case 3: {
        // Empty answer = "keep existing"; otherwise parse triggers.
        const m = message.trim().toLowerCase();
        if (/^(keep|no|just keep|leave (it )?alone|no changes?)/.test(m)) {
          a.additional_triggers = [];
        } else {
          a.additional_triggers = parseTriggers(message);
        }
        break;
      }
    }
    draft.completed_questions.push(next);
  }

  saveDraft(draft);
  return promptNextQuestion(draft);
}

// ──────────────────────────────────────────────────────────────────
// Utility — relative time for the resume prompt
// ──────────────────────────────────────────────────────────────────

function ordinalSuffix(n: number): string {
  const j = n % 10, k = n % 100;
  if (j === 1 && k !== 11) return "st";
  if (j === 2 && k !== 12) return "nd";
  if (j === 3 && k !== 13) return "rd";
  return "th";
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const delta = Math.max(0, Math.floor((now - then) / 1000));
  if (delta < 60) return `${delta} seconds ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)} minutes ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} hours ago`;
  return `${Math.floor(delta / 86400)} days ago`;
}

main().catch((err: unknown) => {
  envelope({
    ok: false,
    phase: "error",
    user_message: "Hit an error elicit-side. Try again in a moment.",
    draft_slug: null,
    detail: { error: err instanceof Error ? err.message : String(err) },
  });
});
