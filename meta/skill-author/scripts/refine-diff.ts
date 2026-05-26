#!/usr/bin/env -S bun run
/**
 * refine-diff.ts — one-shot LLM-driven refine for skill-author.
 *
 * Replaces the verbose R1/R2/R3 elicit flow for refine intent. The user's
 * single message IS the spec; we read the target SKILL.md + eval.yaml,
 * ask Kimi K2.6 to produce a bounded diff, validate it via six guards,
 * and emit a confirmation envelope. On "yes" the existing commit chain
 * (elicit.ts handleConfirmation → commit-and-deploy.ts) ships the new
 * content verbatim — no further regeneration.
 *
 * Modes:
 *   --mode=propose --target=<slug> --description=<text>
 *     Read the target skill, ask Kimi for proposed new content, validate,
 *     write a draft to .context/skill-author-drafts/<slug>.json with
 *     awaiting_confirmation=true and emit the change summary envelope.
 *
 * Input options:
 *   --description=<text>          inline (escape quotes; agent does this)
 *   --description-stdin           read description from stdin
 *
 * On validation failure: emit a fallback envelope suggesting the slower
 * Q&A elicit flow via `/refine <slug>`. We never auto-fix; bad diffs go
 * back to the human.
 *
 * Why a separate script (not a flag in elicit.ts):
 *   • elicit.ts is a pure-deterministic state machine (zero LLM calls);
 *     adding an LLM call there breaks its <50ms latency budget and its
 *     "no model in the loop" safety property.
 *   • Keeps the elicit fallback path intact: refine-diff fails → elicit's
 *     R1/R2/R3 still works.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { join, resolve } from "path";

// ──────────────────────────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────────────────────────

type Tier = "skills" | "openclaw/skills" | "user-skills";

interface RefineDraft {
  schema_version: 1;
  slug: string;
  mode: "refine";
  // Distinguishes a refine-diff draft from a Q&A elicit refine draft.
  // commit-and-deploy.ts keys off the presence of proposed_skill_md to
  // bypass applyRefineDiff() and write the LLM-produced content directly.
  sub_mode: "refine-diff";
  created_at: string;
  updated_at: string;
  // All three "questions" are pre-filled from the LLM output so the
  // existing router logic (active-draft → ingest-answer → confirmation)
  // routes the user's "yes" through elicit.ts → handleConfirmation →
  // runCommitChain without modification.
  completed_questions: number[];
  awaiting_confirmation: true;
  answers: {
    target: string;
    change_statement: string;       // verbatim user description
    desired_behavior: string;       // joined change_summary
    additional_triggers: string[];  // populated only if LLM added triggers
  };
  source_tier: Tier;
  source_sha: string;
  source_triggers: string[];
  // refine-diff-specific fields:
  proposed_skill_md: string;
  proposed_eval_yaml: string;
  change_summary: string[];
}

interface Envelope {
  ok: boolean;
  phase: "elicit" | "safety" | "error";
  user_message: string;
  draft_slug: string | null;
  detail?: Record<string, unknown>;
}

interface ProposalResult {
  proposed_skill_md: string;
  proposed_eval_yaml: string;
  change_summary: string[];
}

// ──────────────────────────────────────────────────────────────────
// Paths + constants
// ──────────────────────────────────────────────────────────────────

const REPO_ROOT = process.env.REPO_ROOT ?? resolve(__dirname, "../../..");
const DRAFT_DIR = join(REPO_ROOT, ".context", "skill-author-drafts");
const SKILLS_DIRS: Tier[] = ["skills", "openclaw/skills", "user-skills"];

// Frontmatter fields that are NEVER editable by the LLM. If the LLM
// returns a SKILL.md whose frontmatter changes any of these, the diff
// is rejected — the user must use the slower elicit path to fork or
// rename a skill explicitly.
const IMMUTABLE_FIELDS = new Set([
  "name",
  "slug",
  "forked_from",
  "created_at",
  "created_by",
  "source",
  "eval",
]);

// Frontmatter fields the LLM IS allowed to change. Any frontmatter key
// in the new SKILL.md that is NOT in this set AND was NOT in the old
// frontmatter is flagged as a hallucinated field and rejects the diff.
const EDITABLE_FIELDS = new Set([
  "description",
  "version",
  "trigger",
  "triggers",
  "cron",
  "mutating",
  "last_authoring_turn",
  "tags",
  "output_format",
  "schedule",
]);

// Bounded-diff size cap: |new - old| ≤ max(BOUND_MIN_BYTES, old * BOUND_PCT)
const BOUND_MIN_BYTES = 200;
const BOUND_PCT = 0.5;

// Section-count delta cap (number of "^## " headings allowed to change)
const MAX_SECTION_DELTA = 1;

// LLM caps
const MAX_CHANGE_SUMMARY_BULLETS = 6;
const MIN_CHANGE_SUMMARY_BULLETS = 1;

// ──────────────────────────────────────────────────────────────────
// CLI parsing
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
// Source resolution
// ──────────────────────────────────────────────────────────────────

function resolveSource(target: string): { tier: Tier; dir: string } | null {
  for (const tier of SKILLS_DIRS) {
    const d = join(REPO_ROOT, tier, target);
    if (existsSync(join(d, "SKILL.md"))) return { tier, dir: d };
  }
  return null;
}

function readSourceTriggers(skillDir: string): string[] {
  const evalPath = join(skillDir, "eval.yaml");
  if (!existsSync(evalPath)) return [];
  const text = readFileSync(evalPath, "utf8");
  const triggers: string[] = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*-?\s*utterance:\s*"?([^"\n]+?)"?\s*$/);
    if (m) triggers.push(m[1]);
  }
  return triggers;
}

function readSourceSha(): string {
  try {
    const child = Bun.spawnSync(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"]);
    return child.stdout.toString().trim() || "unknown";
  } catch {
    return "unknown";
  }
}

// ──────────────────────────────────────────────────────────────────
// Frontmatter extraction
// ──────────────────────────────────────────────────────────────────

function extractFrontmatter(skillMd: string): { fm: Record<string, string>; body: string } {
  const m = skillMd.match(/^---\n([\s\S]+?)\n---\n([\s\S]*)$/);
  if (!m) return { fm: {}, body: skillMd };
  const fm: Record<string, string> = {};
  // Lightweight YAML key-extract — we only need top-level scalars + arrays
  // for guard checks. Multi-line block values are concatenated by joining
  // continuation lines (any line not matching ^[a-zA-Z_]+: is a continuation).
  let currentKey = "";
  for (const rawLine of m[1].split("\n")) {
    const km = rawLine.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$/);
    if (km) {
      currentKey = km[1];
      fm[currentKey] = km[2];
    } else if (currentKey && rawLine.trim()) {
      fm[currentKey] = (fm[currentKey] ?? "") + "\n" + rawLine;
    }
  }
  return { fm, body: m[2] };
}

function countSections(skillMd: string): number {
  // Body-only count of ## headings (the frontmatter `## ` doesn't exist
  // in YAML; if it does, the frontmatter regex captures it and it's harmless).
  const { body } = extractFrontmatter(skillMd);
  return (body.match(/^##\s+/gm) ?? []).length;
}

// ──────────────────────────────────────────────────────────────────
// Bounded-diff validation (the 6 guards from /plan-eng-review D1)
// ──────────────────────────────────────────────────────────────────

interface ValidationResult {
  ok: boolean;
  reason?: string;
  guard?: string;
}

function validateBoundedDiff(
  oldSkillMd: string,
  oldEvalYaml: string,
  newSkillMd: string,
  newEvalYaml: string,
  changeSummary: string[],
): ValidationResult {
  // G1 — immutable frontmatter fields must be byte-identical
  const { fm: oldFm } = extractFrontmatter(oldSkillMd);
  const { fm: newFm } = extractFrontmatter(newSkillMd);
  for (const field of IMMUTABLE_FIELDS) {
    if ((oldFm[field] ?? "") !== (newFm[field] ?? "")) {
      return {
        ok: false,
        guard: "G1",
        reason: `immutable frontmatter field \`${field}\` changed (was \`${oldFm[field]}\`, became \`${newFm[field]}\`)`,
      };
    }
  }

  // G2 — every frontmatter key in the proposed file must be either in
  // the editable allowlist OR already present in the old frontmatter
  // (unchanged keys we don't recognize are tolerated to support skills
  // with custom metadata we haven't catalogued).
  for (const key of Object.keys(newFm)) {
    if (EDITABLE_FIELDS.has(key)) continue;
    if (IMMUTABLE_FIELDS.has(key)) continue; // already checked in G1
    if (key in oldFm) continue; // existing custom field, untouched
    return {
      ok: false,
      guard: "G2",
      reason: `proposed frontmatter introduces unrecognized field \`${key}\``,
    };
  }

  // G3 — size delta cap per file
  const sizeCheck = (label: string, oldLen: number, newLen: number): ValidationResult | null => {
    const limit = Math.max(BOUND_MIN_BYTES, Math.floor(oldLen * BOUND_PCT));
    const delta = Math.abs(newLen - oldLen);
    if (delta > limit) {
      return {
        ok: false,
        guard: "G3",
        reason: `${label} size delta ${delta}b exceeds bound ${limit}b (old=${oldLen}b new=${newLen}b)`,
      };
    }
    return null;
  };
  const skillSizeFail = sizeCheck("SKILL.md", oldSkillMd.length, newSkillMd.length);
  if (skillSizeFail) return skillSizeFail;
  const evalSizeFail = sizeCheck("eval.yaml", oldEvalYaml.length, newEvalYaml.length);
  if (evalSizeFail) return evalSizeFail;

  // G4 — section count must not drift by more than MAX_SECTION_DELTA
  const oldSections = countSections(oldSkillMd);
  const newSections = countSections(newSkillMd);
  if (Math.abs(newSections - oldSections) > MAX_SECTION_DELTA) {
    return {
      ok: false,
      guard: "G4",
      reason: `SKILL.md ## section count drifted by ${Math.abs(newSections - oldSections)} (old=${oldSections} new=${newSections}, max=${MAX_SECTION_DELTA})`,
    };
  }

  // G5 — both files parse as YAML/markdown
  //   SKILL.md: must have a `---\n...\n---` frontmatter block + non-empty body
  //   eval.yaml: must contain `skill:` key and either `triggers:` or `cases:` key
  if (!/^---\n[\s\S]+?\n---\n/.test(newSkillMd)) {
    return { ok: false, guard: "G5", reason: "proposed SKILL.md has no YAML frontmatter block" };
  }
  if (!/\bskill:\s*\S+/m.test(newEvalYaml)) {
    return { ok: false, guard: "G5", reason: "proposed eval.yaml missing required `skill:` key" };
  }
  if (!/\b(triggers|cases):/m.test(newEvalYaml)) {
    return { ok: false, guard: "G5", reason: "proposed eval.yaml missing both `triggers:` and `cases:` keys" };
  }

  // G6 — change_summary bullets present + within bounds (the user-facing
  // disclosure that lets the human catch semantic hallucinations the
  // deterministic guards miss).
  if (changeSummary.length < MIN_CHANGE_SUMMARY_BULLETS) {
    return { ok: false, guard: "G6", reason: "LLM returned empty change_summary" };
  }
  if (changeSummary.length > MAX_CHANGE_SUMMARY_BULLETS) {
    return { ok: false, guard: "G6", reason: `LLM returned ${changeSummary.length} change_summary bullets (max ${MAX_CHANGE_SUMMARY_BULLETS})` };
  }
  // Each bullet should be a non-trivial string (10..240 chars)
  for (const b of changeSummary) {
    if (typeof b !== "string" || b.trim().length < 5) {
      return { ok: false, guard: "G6", reason: "change_summary contains a too-short bullet" };
    }
    if (b.length > 240) {
      return { ok: false, guard: "G6", reason: `change_summary bullet exceeds 240 chars: "${b.slice(0, 80)}..."` };
    }
  }

  return { ok: true };
}

// ──────────────────────────────────────────────────────────────────
// LLM call (Kimi K2.6 via Moonshot)
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

function extractFirstJSON(text: string): Record<string, any> | null {
  if (!text) return null;
  // Strip markdown code fences (```json ... ```), which Kimi often wraps the
  // payload in despite the system prompt saying "raw JSON, no fences". Be
  // permissive: match ```json, ```JSON, ```javascript, or just ```.
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

function buildSystemPrompt(): string {
  return `You are a skill-file refiner for the Alfred Telegram assistant. The user describes a change to an existing skill; you produce the proposed new content of SKILL.md and eval.yaml.

HARD RULES (violating any one of these makes your output rejected and the user has to go through a slow 3-question fallback):

1. Return ONLY a single JSON object with this exact shape, nothing else:
   {
     "proposed_skill_md": "<full new SKILL.md content, including the \`---\` frontmatter block>",
     "proposed_eval_yaml": "<full new eval.yaml content>",
     "change_summary": ["<bullet 1 — concrete, user-facing>", "<bullet 2>", ...]
   }

2. NEVER change these frontmatter fields: name, slug, forked_from, created_at, created_by, source, eval. They are immutable.

3. NEVER introduce new top-level frontmatter keys. You may only edit existing keys OR these whitelisted keys: description, version, trigger, triggers, cron, mutating, last_authoring_turn, tags, output_format, schedule.

4. NEVER restructure the document. Adding or removing more than one \`## \` section is forbidden. Reorder is forbidden.

5. NEVER drop existing insight-pool entries, trigger phrases, eval cases, or other list items UNLESS the user's change request explicitly asks for it. When in doubt, ADD; never DELETE.

6. The change_summary array must contain 1–6 short bullets, each a plain-English description of what changed, written **in friend-voice for an end user**. The user sees these bullets and types "yes" to accept.

   FRIEND-VOICE RULES FOR change_summary BULLETS:
   • Write like a friend confirming a tweak in a text — lowercase fine, contractions fine, brief.
   • NEVER use these terms: "trigger phrase", "trigger", "skill", "slug", "fork", "user-skills", "skills/", "openclaw", "eval", "SKILL.md", "frontmatter", "version", "schema", "cron expression", any file path, any backticked code token.
   • Translate internals to outcomes: instead of "added trigger phrase 'X'", say "I'll also save notes when you say 'X'". Instead of "bumped version to 1.1.0" — don't say it at all (the user doesn't care).
   • Translate cron-style schedules to natural time: instead of "changed cron from 15 18 * * * to 30 20 * * *", say "moved your morning brief to 8:30pm".
   • Speak about what the bot will DO differently, not what file changed. The user knows what they asked for; restate it the way they'd describe it to a friend.

   If you cannot honestly describe the change in <=6 friend-voice bullets, the change is too big — return a single bullet saying "this change is too big to do in one shot — mind walking me through it step by step?".

7. Bump the version field (semver patch by default; minor if you added/changed trigger phrases).

8. If the user's request is ambiguous (e.g. "make it better"), produce a change_summary with one bullet: "ambiguous request — please rephrase or use /refine <slug>" and leave proposed_skill_md/proposed_eval_yaml identical to the current content. The validator rejects identity-diffs gracefully.

9. Output raw JSON — no markdown fences, no explanation, no preamble. Just \`{\`...\`}\`. If you wrap the JSON in \`\`\`json fences the parser will still extract it, but plain JSON is preferred.

10. Your FIRST character of output MUST be \`{\`. Your LAST character of output MUST be \`}\`. Anything before \`{\` or after \`}\` (including newlines, prose, fences) is wasted tokens that risk truncating the actual JSON before it finishes.

11. **CASCADE RULE — inconsistency between frontmatter and body is a BUG**: when you change a value that appears in BOTH the frontmatter and the human-readable body, propagate the change everywhere.
   • If you change \`cron:\` in frontmatter, scan the entire SKILL.md for human-readable mentions of the OLD schedule (e.g. "6:15pm", "6:15 PM IST", "18:15", "every morning at 6:15", "fires at 6:15", "delivered at 18:15") and update each occurrence to match the NEW schedule. The description body, the "## When to fire" section, the "## Capability" section, the "## Output format" section — every place the old time is mentioned must match the new cron.
   • If you change \`trigger:\` in frontmatter, scan the body for any list of trigger phrases and update them too.
   • If you change the description field, propagate any keyword changes into the "## Capability" body section so they don't contradict.
   • If you change cron from 18:15 to 18:30, do NOT leave any text in the body saying "6:15pm". Reader-facing inconsistency between frontmatter and body is unacceptable — the body is part of the contract, not just commentary.

Example: refine request "change time from 6:15pm to 6:30pm" — proposed SKILL.md must have cron \`30 18 * * *\` AND every body mention of "6:15"/"18:15" rewritten to "6:30"/"18:30".`;
}

interface LlmCallResult {
  ok: boolean;
  proposal?: ProposalResult;
  error?: string;
}

async function callKimi(
  systemPrompt: string,
  userPayload: object,
): Promise<LlmCallResult> {
  const apiKey = process.env.MOONSHOT_API_KEY ?? await readVaultKey("MOONSHOT_API_KEY");
  if (!apiKey) return { ok: false, error: "MOONSHOT_API_KEY not available" };

  const userMessage = `Apply the change request to the skill files below. Return the JSON envelope per the system prompt.\n\n\`\`\`json\n${JSON.stringify(userPayload, null, 2)}\n\`\`\``;

  // Kimi K2.6 in thinking mode genuinely takes 20-60s on a full SKILL.md
  // refine (chain-of-thought burns most of the budget). Give the API
  // 90s per attempt via AbortController, with a 120s overall budget that
  // allows one full retry on a transient 5xx without blowing past
  // Telegram's UX patience threshold.
  const PER_REQUEST_TIMEOUT_MS = 90_000;
  const TOTAL_BUDGET_MS = 120_000;
  let lastError = "";
  const start = Date.now();
  while (Date.now() - start < TOTAL_BUDGET_MS) {
    const ctrl = new AbortController();
    const timeoutHandle = setTimeout(() => ctrl.abort(), PER_REQUEST_TIMEOUT_MS);
    try {
      const r = await fetch("https://api.moonshot.ai/v1/chat/completions", {
        method: "POST",
        signal: ctrl.signal,
        headers: {
          "Authorization": `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          // Why moonshot-v1-32k, not kimi-k2.6:
          //   kimi-k2.6 runs in mandatory thinking mode (Moonshot rejects
          //   temperature != 1.0 there) and takes 2-3 minutes per refine on
          //   a 5KB SKILL.md, often with the JSON truncated to
          //   reasoning_content. moonshot-v1-32k is the legacy non-thinking
          //   variant — ~25-50s for the same prompt, supports
          //   response_format json_object, returns clean valid JSON in
          //   `content`. Refine-diff needs deterministic structured edits,
          //   not deep chain-of-thought, so the legacy model is the right
          //   tool. Kimi K2.6 remains primary for the conversational agent
          //   (per identity/MODEL_ROUTING.md) — this is a per-script
          //   override, not a config change.
          model: "moonshot-v1-32k",
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: userMessage },
          ],
          // response_format=json_object forces Moonshot to emit
          // syntactically valid JSON (no unescaped newlines inside string
          // values, no markdown fences). Verified against the live API.
          response_format: { type: "json_object" },
          // 8000 max_tokens easily covers a 5KB SKILL.md round-trip with
          // headroom for change_summary bullets. Observed completion_tokens
          // for paul-graham-insights refine: 1826.
          max_tokens: 8000,
          // Low temperature for deterministic structured output. The
          // legacy moonshot models accept the full 0..1 range; only
          // kimi-k2.6 rejects temperature != 1.0.
          temperature: 0.2,
        }),
      });
      if (r.status >= 500) {
        clearTimeout(timeoutHandle);
        lastError = `${r.status}`;
        await new Promise(res => setTimeout(res, 1500));
        continue;
      }
      if (!r.ok) {
        clearTimeout(timeoutHandle);
        return { ok: false, error: `Moonshot API ${r.status}: ${(await r.text()).slice(0, 300)}` };
      }
      const body = await r.json() as {
        choices?: {
          message?: { content?: string; reasoning_content?: string };
          finish_reason?: string;
        }[];
        usage?: { completion_tokens?: number; total_tokens?: number };
      };
      // Kimi K2.6 returns the actual reply in `content`, with chain-of-thought
      // in `reasoning_content`. When the model exhausts max_tokens during
      // reasoning, content can be empty and the JSON ends up partially in
      // reasoning_content. Try content first, then reasoning_content as a
      // last-resort salvage — extractFirstJSON balances braces so it's safe
      // to feed reasoning prose into it.
      const content = body.choices?.[0]?.message?.content ?? "";
      const reasoning = body.choices?.[0]?.message?.reasoning_content ?? "";
      const finishReason = body.choices?.[0]?.finish_reason ?? "?";
      let json = content ? extractFirstJSON(content) : null;
      if (!json && reasoning) json = extractFirstJSON(reasoning);
      if (!json) {
        return {
          ok: false,
          error: `Could not extract JSON (content=${content.length}b, reasoning=${reasoning.length}b, finish=${finishReason})`,
        };
      }
      if (typeof json.proposed_skill_md !== "string" || typeof json.proposed_eval_yaml !== "string") {
        return { ok: false, error: "Moonshot output missing proposed_skill_md or proposed_eval_yaml" };
      }
      if (!Array.isArray(json.change_summary)) {
        return { ok: false, error: "Moonshot output missing change_summary array" };
      }
      clearTimeout(timeoutHandle);
      return {
        ok: true,
        proposal: {
          proposed_skill_md: json.proposed_skill_md,
          proposed_eval_yaml: json.proposed_eval_yaml,
          change_summary: json.change_summary as string[],
        },
      };
    } catch (e) {
      clearTimeout(timeoutHandle);
      const isAbort = e instanceof Error && e.name === "AbortError";
      lastError = isAbort
        ? `request exceeded ${PER_REQUEST_TIMEOUT_MS}ms per-attempt timeout`
        : (e instanceof Error ? e.message : String(e));
      // For abort/timeout, do NOT retry the SAME slow prompt — fall out
      // and report. Retry only network errors (DNS, ECONNRESET, etc.).
      if (isAbort) break;
      await new Promise(res => setTimeout(res, 1500));
    }
  }
  return { ok: false, error: `Moonshot API call failed after ${Math.round((Date.now() - start) / 1000)}s; last error: ${lastError}` };
}

// ──────────────────────────────────────────────────────────────────
// Draft persistence
// ──────────────────────────────────────────────────────────────────

function ensureDraftDir(): void {
  if (!existsSync(DRAFT_DIR)) mkdirSync(DRAFT_DIR, { recursive: true });
}

function saveDraft(draft: RefineDraft): void {
  ensureDraftDir();
  draft.updated_at = new Date().toISOString();
  writeFileSync(join(DRAFT_DIR, `${draft.slug}.json`), JSON.stringify(draft, null, 2));
}

// ──────────────────────────────────────────────────────────────────
// Envelope helpers
// ──────────────────────────────────────────────────────────────────

function emit(env: Envelope): never {
  console.log(JSON.stringify(env));
  process.exit(0);
}

function buildSummaryMessage(target: string, summary: string[], _isFork: boolean, _sourceTier: Tier): string {
  // Bug O fix (2026-05-26): friend-voice rewrite.
  //
  // OLD copy leaked internal terms: "Here's the change I'll make to
  // `note`", backticked slugs, "fork `note` from `skills/` into
  // `user-skills/` — the original stays untouched", "Reply **yes** to
  // save it, **no** to start over, or **cancel** to throw it away".
  // Every one of those is internal architecture (forking, source tiers,
  // skill slugs, multi-choice yes/no/cancel menus) and shouldn't reach
  // chat.
  //
  // NEW copy: just the bullets the LLM produced (prompt is now tuned to
  // make those bullets jargon-free too) + a one-line save prompt. No
  // slug name, no fork plumbing, no tier paths, no menu of options.
  const bulletsText = summary.map(s => `• ${s}`).join("\n");
  return `${bulletsText}\n\nsave it?`;
}

function fallbackMessage(target: string, reason: string): string {
  return `I tried to make that change in one shot but the diff looked off (${reason}). Let's do it the careful way — say \`/refine ${target}\` and I'll walk you through 3 short questions instead.`;
}

// ──────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);
  const mode = (args["mode"] as string) ?? "propose";

  if (mode !== "propose") {
    emit({
      ok: false,
      phase: "error",
      user_message: `refine-diff: unknown mode \`${mode}\``,
      draft_slug: null,
    });
  }

  const target = args["target"] as string | undefined;
  if (!target) {
    emit({
      ok: false,
      phase: "error",
      user_message: "Which skill should I refine? Try `/refine <name>` or describe what to change.",
      draft_slug: null,
    });
  }

  // Resolve description: --description=... OR --description-stdin
  let description = "";
  if (args["description-stdin"]) {
    description = (await Bun.stdin.text()).trim();
  } else if (typeof args["description"] === "string") {
    description = (args["description"] as string).trim();
  }
  if (!description) {
    emit({
      ok: false,
      phase: "error",
      user_message: `I need a description of what to change. Try: "modify ${target} to also include X".`,
      draft_slug: null,
    });
  }

  // Resolve source skill
  const src = resolveSource(target!);
  if (!src) {
    emit({
      ok: false,
      phase: "error",
      user_message: `I don't see a skill called \`${target}\`. Skills live under \`skills/\`, \`openclaw/skills/\`, or \`user-skills/\`.`,
      draft_slug: null,
    });
  }

  const isFork = src!.tier !== "user-skills";
  const sourceSha = isFork ? readSourceSha() : "current";
  const sourceTriggers = readSourceTriggers(src!.dir);

  // Read current files (whatever tier they're in — refine-diff doesn't
  // pre-fork; it asks the LLM to produce the final content as if the
  // skill already lives in user-skills/, then commit-and-deploy handles
  // the actual fork mechanics on yes).
  const currentSkillMd = readFileSync(join(src!.dir, "SKILL.md"), "utf8");
  const currentEvalYaml = existsSync(join(src!.dir, "eval.yaml"))
    ? readFileSync(join(src!.dir, "eval.yaml"), "utf8")
    : `# auto-generated eval.yaml\nskill: ${target}\nversion: 1\ntriggers: []\n`;

  // Build LLM payload
  const userPayload = {
    target,
    source_tier: src!.tier,
    change_request: description,
    immutable_fields: Array.from(IMMUTABLE_FIELDS),
    editable_fields: Array.from(EDITABLE_FIELDS),
    current_skill_md: currentSkillMd,
    current_eval_yaml: currentEvalYaml,
  };

  const systemPrompt = buildSystemPrompt();
  const llmResult = await callKimi(systemPrompt, userPayload);

  if (!llmResult.ok || !llmResult.proposal) {
    emit({
      ok: false,
      phase: "error",
      user_message: fallbackMessage(target!, `LLM call failed: ${(llmResult.error ?? "unknown").slice(0, 120)}`),
      draft_slug: null,
      detail: { error: llmResult.error, fallback: "elicit_q_and_a" },
    });
  }

  const proposal = llmResult.proposal!;

  // Run the 6 bounded-diff guards
  const validation = validateBoundedDiff(
    currentSkillMd,
    currentEvalYaml,
    proposal.proposed_skill_md,
    proposal.proposed_eval_yaml,
    proposal.change_summary,
  );

  if (!validation.ok) {
    emit({
      ok: false,
      phase: "safety",
      user_message: fallbackMessage(target!, `${validation.guard}: ${validation.reason}`),
      draft_slug: null,
      detail: {
        guard: validation.guard,
        reason: validation.reason,
        fallback: "elicit_q_and_a",
        change_summary_attempted: proposal.change_summary,
      },
    });
  }

  // Save draft
  const draft: RefineDraft = {
    schema_version: 1,
    slug: target!,
    mode: "refine",
    sub_mode: "refine-diff",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    completed_questions: [1, 2, 3],
    awaiting_confirmation: true,
    answers: {
      target: target!,
      change_statement: description,
      desired_behavior: proposal.change_summary.join("; "),
      additional_triggers: [],
    },
    source_tier: src!.tier,
    source_sha: sourceSha,
    source_triggers: sourceTriggers,
    proposed_skill_md: proposal.proposed_skill_md,
    proposed_eval_yaml: proposal.proposed_eval_yaml,
    change_summary: proposal.change_summary,
  };
  saveDraft(draft);

  emit({
    ok: true,
    phase: "elicit",
    user_message: buildSummaryMessage(target!, proposal.change_summary, isFork, src!.tier),
    draft_slug: draft.slug,
    detail: {
      sub_phase: "draft_summary",
      awaiting_confirmation: true,
      sub_mode: "refine-diff",
      fork: isFork,
      source_tier: src!.tier,
      change_summary_count: proposal.change_summary.length,
    },
  });
}

main().catch((err: unknown) => {
  emit({
    ok: false,
    phase: "error",
    user_message: "Hit an error proposing the refine. Try `/refine <name>` for the careful Q&A path.",
    draft_slug: null,
    detail: { error: err instanceof Error ? err.message : String(err) },
  });
});
