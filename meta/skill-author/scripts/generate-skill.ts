#!/usr/bin/env -S bun run
/**
 * generate-skill.ts — LLM polish step that produces production-quality
 * SKILL.md + eval.yaml from a confirmed draft.
 *
 * Today: commit-and-deploy.ts generates SKILL.md by pasting the raw
 * `capability` answer into description AND `## Capability` body. The
 * eval.yaml has empty `triggers: []`. Result: skill files that look
 * like notes-to-self, nothing like skills/agent-browser/SKILL.md or
 * skills/image-ocr/SKILL.md.
 *
 * Tomorrow (this script): right BEFORE commit-and-deploy runs, fire a
 * Moonshot call with the draft answers + an in-context few-shot from
 * the three gold-standard skills. The LLM produces a structured
 * SKILL.md (Capability / How it works / When to fire / Output format /
 * Notes) plus an eval.yaml with 2-4 positive triggers + 1-2 negative
 * triggers. Bounded-diff guards reject unsafe output.
 *
 * Usage:
 *   bun run generate-skill.ts --draft-file=<path>
 *
 * Output: mutates the draft file in-place, adding:
 *   proposed_skill_md_body : string  (just the body, no frontmatter)
 *   proposed_eval_yaml     : string  (full eval.yaml content)
 *   change_summary         : string[] (the bullet-summary the user already saw)
 *
 * Frontmatter generation stays with commit-and-deploy.ts (slug, cron,
 * version, eval path are deterministic). The LLM is responsible only
 * for the human-readable body + eval fixtures.
 *
 * On any failure (LLM error, validation reject), exits 0 without
 * mutating the draft — commit-and-deploy.ts then uses the existing
 * boilerplate generator. No-worse-than-today guarantee.
 */

import { existsSync, readFileSync, writeFileSync } from "fs";
import { join, resolve } from "path";

// ──────────────────────────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────────────────────────

interface CreateAnswers {
  capability?: string;
  invocation_mode?: "manual" | "periodic";
  triggers?: string[];
  schedule?: {
    frequency?: string;
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
  mode: "create" | "refine";
  sub_mode?: string;
  created_at: string;
  updated_at: string;
  completed_questions: number[];
  awaiting_confirmation?: boolean;
  answers: any;
  // Populated by this script:
  proposed_skill_md_body?: string;
  proposed_eval_yaml?: string;
  // intake-from-context.ts already stores chat context; we re-use it
  // when the draft was created via that path, otherwise an empty array.
  intake_audit?: {
    session_file?: string;
    messages_used?: number;
    llm_extracted?: any;
  };
}

interface GeneratorOutput {
  skill_md_body: string;
  eval_yaml: string;
}

// ──────────────────────────────────────────────────────────────────
// Constants
// ──────────────────────────────────────────────────────────────────

const REPO_ROOT = process.env.REPO_ROOT ?? resolve(__dirname, "../../..");
// Bounded-diff sizing — generated SKILL.md body should be 800-6000 chars,
// eval.yaml 200-2500 chars. Outside these bounds we reject the proposal.
const SKILL_MD_MIN = 400;
const SKILL_MD_MAX = 8000;
const EVAL_YAML_MIN = 150;
const EVAL_YAML_MAX = 4000;
// Section-count expectations
const SKILL_MD_MIN_SECTIONS = 3;
const SKILL_MD_MAX_SECTIONS = 8;
// Fixture-count expectations
const MIN_POSITIVE_FIXTURES = 2;
const MIN_NEGATIVE_FIXTURES = 1;

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
// LLM call
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

const SYSTEM_PROMPT = `You produce production-quality SKILL.md body content and eval.yaml fixtures for the Alfred personal assistant. The user just confirmed they want to ship a new skill. Your output is the FINAL polished version that gets committed to GitHub.

Conventions to match (from existing skills/agent-browser, skills/image-ocr, skills/ingest-pdf):

SKILL.md BODY (no frontmatter — that's generated separately):

# /<slug>

Author: Alfred (Telegram skill-author, <YYYY-MM-DD>)

## Capability

One short paragraph (2-4 sentences). What this skill does, written cleanly. Distill from the user's request — DO NOT paste their raw informal phrasing. Use imperative voice, concrete nouns. Example tone: "Polls Y Combinator's launch page once per day at 6:15 PM IST and delivers the new-batch companies as a bullet list, each line with the company name, a one-line description, and the YC launch URL."

## How it works

A bulleted list of the concrete steps. 3-6 bullets max. Be specific about data sources, processing, and delivery. Example:
- Polls https://www.ycombinator.com/launches once per day at the scheduled time
- Filters to companies with launch_date >= today - 1 day AND batch == latest YC batch
- Formats each as "**<name>** — <one-line description>" with the YC URL on the next line
- Delivers as a Telegram message (HTML parse_mode)

## When to fire

For periodic skills: the schedule in plain English ("Fires once per day at 18:15 IST.") plus the cron expression in backticks. Use the cron_expression field from the input payload VERBATIM — do not re-derive it. If there are optional manual trigger phrases, list them as bullets.

For manual skills: a bulleted list of the trigger phrases the user mentioned, plus context cues (e.g., "User says any of: ..." or "When user forwards a PDF, ..."). End with a "NOT WHEN" disambiguation if applicable.

## Output format

Show a CONCRETE EXAMPLE of what the user will see. Use a fenced code block if the output is structured. Match the format from the chat context if there's one (e.g., the bot already showed a sample). Don't invent placeholder data — use realistic content shapes.

## Notes

Three bullets exactly:
- Authored from Telegram on <YYYY-MM-DD>.
- Edit by saying \`/refine <slug>\` in Telegram, or hand-edit this file in Claude Code.
- See \`brain/skills/user-skills/<slug>.md\` for the original elicit Q&A.

---

eval.yaml STRUCTURE:

# Auto-generated by meta/skill-author on <ISO timestamp>
skill: <slug>
version: 1

triggers:
  # POSITIVE: 2-4 example utterances that should invoke this skill.
  - id: <slug>-001
    utterance: "<concrete phrase the user would type>"
    expect_skill: <slug>
    notes: <optional 1-line disambiguation>
  - id: <slug>-002
    utterance: "<a different phrasing of the same intent>"
    expect_skill: <slug>
  # NEGATIVE: 1-2 utterances that look similar but should NOT invoke.
  - id: <slug>-neg-001
    utterance: "<a sibling-skill request or off-topic phrase>"
    expect_skill: null
    notes: <why this is excluded>

schedule: "<daily|weekly|hourly|monthly|manual|custom>"

---

CRITICAL RULES:

1. Return ONLY a JSON object:
   { "skill_md_body": "<the body, starting from '# /<slug>'>", "eval_yaml": "<the full eval.yaml content>" }
2. NO frontmatter in skill_md_body — that's added separately. Start with '# /<slug>'.
3. Distill the Capability section. Do NOT paste the user's raw input. Clean it up.
4. eval.yaml MUST have >= 2 positive triggers and >= 1 negative trigger.
5. For periodic skills, "## How it works" MUST mention the data source / fetching strategy. If the data source is unclear, write "User's brain pages" or "External API (see implementation)".
6. The "## Output format" section MUST show a concrete example, not a placeholder like "<output here>".
7. Output raw JSON. First char '{', last char '}'. No markdown fences.

Your output replaces a previous boilerplate generator that just pasted the user's raw request into both description and ## Capability. Anything that level of quality or worse fails the build. The reader is a future engineer (the user himself) reading this 3 months from now.`;

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

async function callKimi(draft: Draft): Promise<{ ok: boolean; gen?: GeneratorOutput; error?: string }> {
  const apiKey = process.env.MOONSHOT_API_KEY ?? await readVaultKey("MOONSHOT_API_KEY");
  if (!apiKey) return { ok: false, error: "MOONSHOT_API_KEY not available" };

  const a: CreateAnswers = draft.answers ?? {};
  // Slug we'd derive from capability — informational only, the actual
  // slug is decided by commit-and-deploy.ts (handles collision auto-bump).
  const provisionalSlug = slugify(a.capability ?? "new-skill");

  // Pull chat context if the draft came from intake-from-context. Helps the
  // LLM write a realistic "## Output format" example when the bot already
  // showed a sample in chat.
  let chatContextHint = "";
  if (draft.intake_audit?.session_file) {
    chatContextHint = `The draft was extracted from a prior chat. Use that chat context (already encoded in the answers below) to make the Output format example REALISTIC — if the bot showed a sample format, mirror it.`;
  }

  // Compute the actual cron expression from schedule answers so the LLM
  // doesn't have to derive it (and get it subtly wrong — observed
  // 2026-05-21: LLM emitted `0 18 * * *` for an 18:15 schedule). The
  // generated body MUST use this cron verbatim — frontmatter is set
  // deterministically by commit-and-deploy from the same answers, and
  // we don't want body/frontmatter to disagree.
  const cronExpr = a.invocation_mode === "periodic" ? computeCron(a.schedule) : null;

  const payload = {
    slug: provisionalSlug,
    today_iso_date: new Date().toISOString().slice(0, 10),
    answers: a,
    // Pre-computed cron — the LLM should paste this exactly in the body
    // when generating the "When to fire" section. Set to null for
    // manual-mode skills.
    cron_expression: cronExpr,
    chat_context_hint: chatContextHint,
    // If the draft came from intake, surface the extracted-from chat for
    // few-shot. Trim to keep prompt size sane.
    intake_extracted: draft.intake_audit?.llm_extracted ?? null,
  };

  const userMessage = `Generate the SKILL.md body and eval.yaml for this confirmed skill. The slug shown is provisional; the final commit step picks the real slug. Return JSON only.\n\n\`\`\`json\n${JSON.stringify(payload, null, 2)}\n\`\`\``;

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
            { role: "system", content: SYSTEM_PROMPT },
            { role: "user", content: userMessage },
          ],
          response_format: { type: "json_object" },
          max_tokens: 8000,
          temperature: 0.3,
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
      const body = await r.json() as { choices?: { message?: { content?: string; reasoning_content?: string } }[] };
      const content = body.choices?.[0]?.message?.content ?? "";
      const reasoning = body.choices?.[0]?.message?.reasoning_content ?? "";
      let json = content ? extractFirstJSON(content) : null;
      if (!json && reasoning) json = extractFirstJSON(reasoning);
      clearTimeout(t);
      if (!json) {
        return { ok: false, error: `Could not parse JSON (content=${content.length}b reasoning=${reasoning.length}b)` };
      }
      if (typeof json.skill_md_body !== "string" || typeof json.eval_yaml !== "string") {
        return { ok: false, error: "Output missing skill_md_body or eval_yaml" };
      }
      return {
        ok: true,
        gen: {
          skill_md_body: json.skill_md_body,
          eval_yaml: json.eval_yaml,
        },
      };
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
  return { ok: false, error: `Moonshot call failed after ${Math.round((Date.now() - start) / 1000)}s; last error: ${lastError}` };
}

// ──────────────────────────────────────────────────────────────────
// Validation
// ──────────────────────────────────────────────────────────────────

function countSections(md: string): number {
  return (md.match(/^##\s+/gm) ?? []).length;
}

function countFixtures(evalYaml: string): { positive: number; negative: number } {
  const lines = evalYaml.split("\n");
  let positive = 0;
  let negative = 0;
  for (const line of lines) {
    const m = line.match(/^\s*expect_skill:\s*(.+)$/);
    if (!m) continue;
    const val = m[1].trim();
    if (val === "null" || val === "~" || val === '""') negative++;
    else positive++;
  }
  return { positive, negative };
}

function validateGenerated(gen: GeneratorOutput): { ok: boolean; reason?: string } {
  // Size bounds
  if (gen.skill_md_body.length < SKILL_MD_MIN) {
    return { ok: false, reason: `SKILL.md body too short (${gen.skill_md_body.length} < ${SKILL_MD_MIN})` };
  }
  if (gen.skill_md_body.length > SKILL_MD_MAX) {
    return { ok: false, reason: `SKILL.md body too long (${gen.skill_md_body.length} > ${SKILL_MD_MAX})` };
  }
  if (gen.eval_yaml.length < EVAL_YAML_MIN) {
    return { ok: false, reason: `eval.yaml too short (${gen.eval_yaml.length} < ${EVAL_YAML_MIN})` };
  }
  if (gen.eval_yaml.length > EVAL_YAML_MAX) {
    return { ok: false, reason: `eval.yaml too long (${gen.eval_yaml.length} > ${EVAL_YAML_MAX})` };
  }
  // Section count
  const sections = countSections(gen.skill_md_body);
  if (sections < SKILL_MD_MIN_SECTIONS) {
    return { ok: false, reason: `SKILL.md has ${sections} ## sections (min ${SKILL_MD_MIN_SECTIONS})` };
  }
  if (sections > SKILL_MD_MAX_SECTIONS) {
    return { ok: false, reason: `SKILL.md has ${sections} ## sections (max ${SKILL_MD_MAX_SECTIONS})` };
  }
  // Must start with the # /<slug> header
  if (!/^#\s+\/[a-z0-9-]+/m.test(gen.skill_md_body)) {
    return { ok: false, reason: "SKILL.md body missing leading '# /<slug>' header" };
  }
  // eval.yaml structural check
  if (!/^\s*skill:\s*\S+/m.test(gen.eval_yaml)) {
    return { ok: false, reason: "eval.yaml missing 'skill:' key" };
  }
  if (!/^\s*triggers:/m.test(gen.eval_yaml)) {
    return { ok: false, reason: "eval.yaml missing 'triggers:' key" };
  }
  const fixtures = countFixtures(gen.eval_yaml);
  if (fixtures.positive < MIN_POSITIVE_FIXTURES) {
    return { ok: false, reason: `eval.yaml has ${fixtures.positive} positive fixtures (min ${MIN_POSITIVE_FIXTURES})` };
  }
  if (fixtures.negative < MIN_NEGATIVE_FIXTURES) {
    return { ok: false, reason: `eval.yaml has ${fixtures.negative} negative fixtures (min ${MIN_NEGATIVE_FIXTURES})` };
  }
  return { ok: true };
}

// ──────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────

function computeCron(sched: any): string {
  if (!sched) return "* * * * *";
  if (sched.frequency === "custom") return sched.custom_cron ?? "* * * * *";
  const tod = sched.time_of_day ?? "00:00";
  const [hh, mm] = tod.startsWith(":")
    ? ["*", tod.slice(1)]
    : tod.split(":");
  switch (sched.frequency) {
    case "hourly": return `${mm || "0"} * * * *`;
    case "daily": return `${mm} ${hh} * * *`;
    case "weekly": {
      const dow = ({ sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 } as Record<string, number>)[(sched.day_of_week ?? "mon").slice(0, 3).toLowerCase()] ?? 1;
      return `${mm} ${hh} * * ${dow}`;
    }
    case "monthly": return `${mm} ${hh} ${sched.day_of_month ?? 1} * *`;
    default: return "* * * * *";
  }
}

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
// Main
// ──────────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv);
  const draftPath = args["draft-file"] as string;
  if (!draftPath || !existsSync(draftPath)) {
    console.error(`generate-skill: draft file not found: ${draftPath}`);
    process.exit(2);
  }
  let draft: Draft;
  try {
    draft = JSON.parse(readFileSync(draftPath, "utf8")) as Draft;
  } catch (e) {
    console.error(`generate-skill: malformed draft JSON: ${e}`);
    process.exit(2);
  }

  // Only operate on create drafts. Refine has its own diff script.
  if (draft.mode !== "create") {
    // No-op for refine drafts — silently exit 0 so the caller doesn't have
    // to special-case.
    process.exit(0);
  }

  const result = await callKimi(draft);
  if (!result.ok || !result.gen) {
    // Soft failure — commit-and-deploy will fall back to boilerplate.
    console.error(`generate-skill: LLM call failed: ${result.error}`);
    process.exit(0);
  }

  const validation = validateGenerated(result.gen);
  if (!validation.ok) {
    console.error(`generate-skill: validation rejected: ${validation.reason}`);
    process.exit(0);
  }

  // Mutate draft in place
  draft.proposed_skill_md_body = result.gen.skill_md_body;
  draft.proposed_eval_yaml = result.gen.eval_yaml;
  draft.updated_at = new Date().toISOString();
  writeFileSync(draftPath, JSON.stringify(draft, null, 2));

  console.log(`generate-skill: ok — skill_md_body=${result.gen.skill_md_body.length}b eval_yaml=${result.gen.eval_yaml.length}b`);
  process.exit(0);
}

main().catch((err: unknown) => {
  // Never block the commit pipeline — exit 0 on any uncaught error,
  // commit-and-deploy uses boilerplate.
  console.error(`generate-skill: uncaught error: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(0);
});
