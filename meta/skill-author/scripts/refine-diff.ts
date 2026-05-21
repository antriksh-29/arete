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
  const start = text.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (escape) { escape = false; continue; }
    if (ch === "\\") { escape = true; continue; }
    if (ch === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(text.slice(start, i + 1)); } catch { return null; }
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

6. The change_summary array must contain 1–6 short bullets, each a plain-English description of what changed. The user sees these bullets and types "yes" to accept. If you cannot honestly describe the change in <=6 bullets, the change is too big — return a single bullet saying "change too broad for one-shot refine; please use /refine ${"<slug>"} for the Q&A path".

7. Bump the version field (semver patch by default; minor if you added/changed trigger phrases).

8. If the user's request is ambiguous (e.g. "make it better"), produce a change_summary with one bullet: "ambiguous request — please rephrase or use /refine <slug>" and leave proposed_skill_md/proposed_eval_yaml identical to the current content. The validator rejects identity-diffs gracefully.

9. Output raw JSON — no markdown fences, no explanation, no preamble. Just \`{\`...\`}\`.`;
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

  // Same retry shape as safety-scan.ts — 3s total budget, retry on 5xx.
  let lastError = "";
  const start = Date.now();
  while (Date.now() - start < 8000) {
    try {
      const r = await fetch("https://api.moonshot.ai/v1/chat/completions", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: "kimi-k2.6",
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: userMessage },
          ],
          // Allow enough room for a SKILL.md + eval.yaml round-trip.
          // A typical user-skill SKILL.md is ~3-6KB; we double + buffer.
          max_tokens: 8192,
          temperature: 0.0,
        }),
      });
      if (r.status >= 500) {
        lastError = `${r.status}`;
        await new Promise(res => setTimeout(res, 800));
        continue;
      }
      if (!r.ok) {
        return { ok: false, error: `Moonshot API ${r.status}: ${(await r.text()).slice(0, 300)}` };
      }
      const body = await r.json() as { choices?: { message?: { content?: string } }[] };
      const text = body.choices?.[0]?.message?.content;
      if (!text) return { ok: false, error: "Moonshot returned no content" };
      const json = extractFirstJSON(text);
      if (!json) return { ok: false, error: "Could not extract JSON from Moonshot reply" };
      if (typeof json.proposed_skill_md !== "string" || typeof json.proposed_eval_yaml !== "string") {
        return { ok: false, error: "Moonshot output missing proposed_skill_md or proposed_eval_yaml" };
      }
      if (!Array.isArray(json.change_summary)) {
        return { ok: false, error: "Moonshot output missing change_summary array" };
      }
      return {
        ok: true,
        proposal: {
          proposed_skill_md: json.proposed_skill_md,
          proposed_eval_yaml: json.proposed_eval_yaml,
          change_summary: json.change_summary as string[],
        },
      };
    } catch (e) {
      lastError = e instanceof Error ? e.message : String(e);
      await new Promise(res => setTimeout(res, 800));
    }
  }
  return { ok: false, error: `Moonshot API unreachable for 8s; last error: ${lastError}` };
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

function buildSummaryMessage(target: string, summary: string[], isFork: boolean, sourceTier: Tier): string {
  const bulletsText = summary.map(s => `• ${s}`).join("\n");
  const forkLine = isFork
    ? `\n\n(This will **fork** \`${target}\` from \`${sourceTier}/\` into \`user-skills/\` — the original stays untouched.)`
    : "";
  return `Here's the change I'll make to \`${target}\`:\n\n${bulletsText}${forkLine}\n\nReply **yes** to save it, **no** to start over, or **cancel** to throw it away.`;
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
