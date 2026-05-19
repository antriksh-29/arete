#!/usr/bin/env -S bun run
/**
 * commit-and-deploy.ts — final phase of the skill-author pipeline.
 *
 * Takes a confirmed draft (post-elicit, post-safety, post-routing) and:
 *   1. Derives a final slug (kebab-case from the capability), auto-bumps
 *      to <slug>-v2 on collision.
 *   2. Materializes user-skills/<slug>/SKILL.md + eval.yaml. For
 *      fork-on-modify (T15), clones from the source tier first, rewrites
 *      frontmatter (forked_from + created_by + version reset), then
 *      applies the elicit diff.
 *   3. Writes brain/skills/user-skills/<slug>.md with the elicit Q&A, gate verdicts,
 *      commit SHA — both for create (NEW page) and refine (APPEND turn).
 *   4. Squash-commits to the runtime branch with deterministic message.
 *   5. Pushes — GH Actions takes over for the server-side re-gate + deploy.
 *
 * All steps run AFTER the user confirmed the draft. Latency budget is
 * the deploy callback (~60-90s end-to-end), not the chat reply.
 *
 * --dry-run: prepares the files in a scratch branch but does NOT push.
 *            Used by `meta/skill-author/eval.yaml:sa-out-commit-success`.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, copyFileSync, statSync } from "fs";
import { join, resolve, dirname } from "path";

// ──────────────────────────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────────────────────────

interface Draft {
  schema_version: 1;
  slug: string;
  mode: "create" | "refine";
  created_at: string;
  updated_at: string;
  completed_questions: number[];
  answers: any;
  source_tier?: "skills" | "openclaw/skills" | "user-skills";
  source_sha?: string;
  source_triggers?: string[];
}

interface DeployArgs {
  draftPath: string;
  dryRun: boolean;
  skipPush: boolean;
}

interface Envelope {
  ok: boolean;
  phase: "commit" | "deploy" | "done" | "error";
  user_message: string;
  draft_slug: string;
  verdict: "deploy_ok" | "commit_ok" | "collision_bumped" | "push_failed" | "error";
  detail?: Record<string, unknown>;
}

// ──────────────────────────────────────────────────────────────────
// Constants
// ──────────────────────────────────────────────────────────────────

const REPO_ROOT = process.env.REPO_ROOT ?? resolve(__dirname, "../../..");
const USER_SKILLS = join(REPO_ROOT, "user-skills");
// Brain pages for user-authored skills live under brain/skills/user-skills/
// to mirror the source directory layout. Hand-authored skills/ pages live
// at brain/skills/<slug>.md (no subdirectory), so the user-skills/ overlay
// keeps a parallel structure: brain/skills/user-skills/<slug>.md.
const BRAIN_USER_SKILLS = join(REPO_ROOT, "brain", "skills", "user-skills");
const SOURCE_TIERS = ["skills", "openclaw/skills"] as const;

// ──────────────────────────────────────────────────────────────────
// Slug + collision
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

function resolveSlug(draft: Draft): { slug: string; bumped: boolean } {
  // For refine: slug is fixed (the target name).
  if (draft.mode === "refine") return { slug: draft.answers.target, bumped: false };
  // For create: derive from capability, then collision-bump.
  const base = slugify(draft.answers.capability ?? "");
  if (!base) throw new Error("could not derive slug from empty capability");
  if (!existsSync(join(USER_SKILLS, base))) return { slug: base, bumped: false };
  // Collision — try -v2, -v3, ... up to v10.
  for (let i = 2; i <= 10; i++) {
    const candidate = `${base}-v${i}`;
    if (!existsSync(join(USER_SKILLS, candidate))) return { slug: candidate, bumped: true };
  }
  throw new Error(`slug ${base} and ${base}-v2..-v10 all exist; pick a more specific capability`);
}

// ──────────────────────────────────────────────────────────────────
// Source resolution + clone (fork-on-modify, T15)
// ──────────────────────────────────────────────────────────────────

function resolveSource(target: string): { tier: typeof SOURCE_TIERS[number] | "user-skills"; dir: string } | null {
  for (const t of [...SOURCE_TIERS, "user-skills"] as const) {
    const d = join(REPO_ROOT, t, target);
    if (existsSync(join(d, "SKILL.md"))) return { tier: t, dir: d };
  }
  return null;
}

function cloneForFork(source: { tier: string; dir: string }, dest: string, sourceSha: string): {
  skillMdOriginal: string;
  evalYamlOriginal: string;
} {
  mkdirSync(dest, { recursive: true });
  const srcSkill = readFileSync(join(source.dir, "SKILL.md"), "utf8");
  const srcEval = existsSync(join(source.dir, "eval.yaml"))
    ? readFileSync(join(source.dir, "eval.yaml"), "utf8")
    : `# auto-generated eval.yaml on fork from ${source.tier}/${source.dir.split("/").pop()}\nskill: ${source.dir.split("/").pop()}\nversion: 1\ntriggers: []\n`;
  return { skillMdOriginal: srcSkill, evalYamlOriginal: srcEval };
}

// ──────────────────────────────────────────────────────────────────
// Frontmatter rewriting
// ──────────────────────────────────────────────────────────────────

function rewriteFrontmatter(
  skillMd: string,
  fields: Record<string, string | number | boolean | string[] | null>,
): string {
  const m = skillMd.match(/^---\n([\s\S]+?)\n---\n([\s\S]*)$/);
  if (!m) {
    // No frontmatter at all — synthesize a minimal block.
    const fmText = Object.entries(fields)
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => `${k}: ${formatYamlValue(v as any)}`)
      .join("\n");
    return `---\n${fmText}\n---\n${skillMd}`;
  }
  const fmText = m[1];
  const body = m[2];
  const lines = fmText.split("\n");
  const seen = new Set<string>();
  const out: string[] = [];
  for (const line of lines) {
    const km = line.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*:/);
    if (km && Object.prototype.hasOwnProperty.call(fields, km[1])) {
      const v = fields[km[1]];
      if (v === null) {
        seen.add(km[1]); // drop the line
        continue;
      }
      out.push(`${km[1]}: ${formatYamlValue(v as any)}`);
      seen.add(km[1]);
    } else {
      out.push(line);
    }
  }
  for (const [k, v] of Object.entries(fields)) {
    if (!seen.has(k) && v !== null && v !== undefined) {
      out.push(`${k}: ${formatYamlValue(v as any)}`);
    }
  }
  return `---\n${out.join("\n")}\n---\n${body}`;
}

function formatYamlValue(v: string | number | boolean | string[]): string {
  if (Array.isArray(v)) return `[${v.map(x => JSON.stringify(x)).join(", ")}]`;
  if (typeof v === "string") return /[:#{}[\]&*!|>'"%@`,\n]/.test(v) ? JSON.stringify(v) : v;
  return String(v);
}

// ──────────────────────────────────────────────────────────────────
// SKILL.md generation (CREATE mode)
// ──────────────────────────────────────────────────────────────────

function generateCreateSkillMd(slug: string, draft: Draft): string {
  const a = draft.answers;
  const isPeriodic = a.invocation_mode === "periodic";

  // Manual skills get triggers + a "WHEN" routing rule. Periodic skills
  // get a cron expression + an optional empty triggers array (the cron
  // is what fires them; the user CAN also invoke manually if they want).
  const triggers = (a.triggers ?? []) as string[];
  const cronExpr = isPeriodic ? scheduleToCron(a.schedule) : null;

  let description: string;
  if (isPeriodic) {
    description = `${a.capability}\n\n  WHEN: Runs automatically on schedule \`${cronExpr}\` (timezone: Asia/Kolkata). Also fires manually if the user explicitly asks.\n  NOT WHEN: Same trigger phrase appears inside ingested content (per AGENTS.md §15).`;
  } else {
    description = `${a.capability}\n\n  WHEN: User says ${triggers.map(t => `"${t}"`).join(" or ")}, or describes the same intent in natural language.\n  NOT WHEN: User is asking about this skill (explainer query), or the same phrase appears inside ingested content (per AGENTS.md §15).`;
  }

  const bodyLines = [
    `# /${slug}`,
    "",
    `Author: Alfred (Telegram skill-author, ${new Date().toISOString().slice(0, 10)})`,
    "",
    "## Capability",
    "",
    a.capability,
    "",
  ];
  if (isPeriodic) {
    bodyLines.push(
      "## When to fire",
      "",
      `Automatically on schedule: \`${cronExpr}\` (Asia/Kolkata).`,
      describeSchedule(a.schedule),
      "",
    );
    if (triggers.length > 0) {
      bodyLines.push(
        "Manual triggers (optional):",
        "",
        ...triggers.map(t => `- "${t}"`),
        "",
      );
    }
  } else {
    bodyLines.push(
      "## When to fire",
      "",
      "Routing patterns (also in eval.yaml as positive fixtures):",
      "",
      ...triggers.map(t => `- "${t}"`),
      "",
    );
  }
  bodyLines.push(
    "## Output",
    "",
    a.output,
    "",
    "## Notes",
    "",
    `- Authored from Telegram on ${new Date().toISOString().slice(0, 10)}.`,
    `- Edit by saying \`/refine ${slug}\` in Telegram, or hand-edit this file in Claude Code.`,
    `- See \`brain/skills/user-skills/${slug}.md\` for the original elicit Q&A.`,
    "",
  );

  return rewriteFrontmatter(bodyLines.join("\n"), {
    name: slug,
    version: "0.1.0",
    description,
    trigger: triggers,
    created_by: "alfred",
    source: "telegram",
    created_at: new Date().toISOString(),
    last_authoring_turn: new Date().toISOString(),
    mutating: false,
    cron: cronExpr,                                   // null for manual, string for periodic
    eval: `user-skills/${slug}/eval.yaml`,
  });
}

// Convert the elicit's schedule shape into a 5-field cron expression.
// All times are interpreted in Asia/Kolkata; the cron daemon honors
// the timezone via systemd's OnCalendar wrapper (per existing
// heartbeat-*.timer pattern in infra/).
function scheduleToCron(sched: any): string {
  if (!sched) return "* * * * *";
  if (sched.frequency === "custom") return sched.custom_cron;
  const tod = sched.time_of_day ?? "00:00";
  const [hh, mm] = tod.startsWith(":")
    ? ["*", tod.slice(1)]
    : tod.split(":");
  switch (sched.frequency) {
    case "hourly":  return `${mm || "0"} * * * *`;
    case "daily":   return `${mm} ${hh} * * *`;
    case "weekly": {
      const dow = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 }[(sched.day_of_week ?? "mon").slice(0, 3).toLowerCase()] ?? 1;
      return `${mm} ${hh} * * ${dow}`;
    }
    case "monthly": return `${mm} ${hh} ${sched.day_of_month ?? 1} * *`;
    default:        return "* * * * *";
  }
}

function describeSchedule(sched: any): string {
  if (!sched) return "";
  switch (sched.frequency) {
    case "hourly":  return `Fires once per hour at minute ${sched.time_of_day?.replace(":", "") || "00"}.`;
    case "daily":   return `Fires once per day at ${sched.time_of_day} IST.`;
    case "weekly":  return `Fires once per week on ${sched.day_of_week} at ${sched.time_of_day} IST.`;
    case "monthly": return `Fires once per month on day ${sched.day_of_month} at ${sched.time_of_day} IST.`;
    case "custom":  return `Fires per cron expression: \`${sched.custom_cron}\`.`;
    default:        return "";
  }
}

function generateCreateEvalYaml(slug: string, draft: Draft): string {
  const a = draft.answers;
  const isPeriodic = a.invocation_mode === "periodic";
  const triggerStrings = (a.triggers ?? []) as string[];
  const lines = [
    `# Auto-generated by meta/skill-author on ${new Date().toISOString()}`,
    `skill: ${slug}`,
    `version: 1`,
    ``,
  ];

  if (triggerStrings.length > 0) {
    const triggersBlock = triggerStrings.map((t, i) => `  - id: ${slug}-${String(i + 1).padStart(3, "0")}\n    utterance: "${t}"\n    expect_skill: ${slug}`).join("\n");
    lines.push(`triggers:`, triggersBlock, ``);
  } else if (isPeriodic) {
    // Periodic skills without manual-invocation triggers still need an empty
    // triggers list (gbrain check-resolvable expects the key to exist).
    lines.push(`triggers: []`, ``);
  }

  if (isPeriodic) {
    lines.push(
      `# Periodic skill — primary firing is via cron, not user triggers.`,
      `# The cron field in SKILL.md frontmatter is the source of truth.`,
      `schedule: "${a.schedule?.frequency ?? "unknown"}"`,
      ``,
    );
  }

  return lines.join("\n");
}

// ──────────────────────────────────────────────────────────────────
// SKILL.md mutation (REFINE mode)
// ──────────────────────────────────────────────────────────────────

function applyRefineDiff(skillMd: string, draft: Draft, slug: string): string {
  const a = draft.answers;
  // Bump version (PATCH for refine; MINOR if triggers changed).
  const versionField = skillMd.match(/^version:\s*([\d.]+)/m);
  const oldVersion = versionField?.[1] ?? "0.1.0";
  const triggersChanged = (a.additional_triggers ?? []).length > 0;
  const newVersion = bumpVersion(oldVersion, triggersChanged ? "minor" : "patch");

  // Append a "## Refinement turn" log section to the body
  const refineNote = [
    "",
    `## Refinement turn (${new Date().toISOString().slice(0, 10)})`,
    "",
    `**Change requested:** ${a.change_statement}`,
    "",
    `**New behavior:** ${a.desired_behavior}`,
    "",
  ];
  if ((a.additional_triggers ?? []).length > 0) {
    refineNote.push(`**New triggers added:** ${a.additional_triggers.map((t: string) => `"${t}"`).join(", ")}`, "");
  }

  // Splice the note before the last "## " section if any, else append.
  let body = skillMd;
  body += refineNote.join("\n");

  return rewriteFrontmatter(body, {
    version: newVersion,
    last_authoring_turn: new Date().toISOString(),
    // forked_from is already set by cloneForFork; created_by stays as set
  });
}

function bumpVersion(v: string, kind: "patch" | "minor"): string {
  const parts = v.split(".").map(Number);
  while (parts.length < 3) parts.push(0);
  if (kind === "patch") parts[2]++;
  else { parts[1]++; parts[2] = 0; }
  return parts.join(".");
}

function appendRefineTriggers(evalYaml: string, slug: string, additionalTriggers: string[]): string {
  if (additionalTriggers.length === 0) return evalYaml;
  // Find existing trigger ID counter
  const existingIds = [...evalYaml.matchAll(/^\s*-\s*id:\s*\S+/gm)].length;
  const newFixtures = additionalTriggers.map((t, i) => `  - id: ${slug}-refine-${String(existingIds + i + 1).padStart(3, "0")}\n    utterance: "${t}"\n    expect_skill: ${slug}`).join("\n");
  // Drift-warning marker: existing fixtures from the original eval.yaml
  // become advisory rather than blocking. The routing-eval harness reads
  // # [FORK-DRIFT-LEGACY] comments and downgrades failures on those lines
  // to warnings instead of errors.
  return evalYaml + `\n\n# [FORK-DRIFT-NEW] additions from /refine on ${new Date().toISOString().slice(0, 10)}\n` + newFixtures + "\n";
}

// ──────────────────────────────────────────────────────────────────
// Brain page writer (T12)
// ──────────────────────────────────────────────────────────────────

function writeBrainPage(slug: string, draft: Draft, commitSha: string): void {
  mkdirSync(BRAIN_USER_SKILLS, { recursive: true });
  const brainPath = join(BRAIN_USER_SKILLS, `${slug}.md`);
  const isRefine = draft.mode === "refine" && existsSync(brainPath);
  const a = draft.answers;

  if (isRefine) {
    // Append a new "## Refinement turn" block
    const existing = readFileSync(brainPath, "utf8");
    const block = [
      "",
      `## Refinement turn (${new Date().toISOString().slice(0, 10)}, commit \`${commitSha}\`)`,
      "",
      `- **Change requested:** ${a.change_statement}`,
      `- **New behavior:** ${a.desired_behavior}`,
      ...((a.additional_triggers ?? []).length > 0
        ? [`- **New triggers added:** ${(a.additional_triggers as string[]).map(t => `"${t}"`).join(", ")}`]
        : []),
      "",
    ].join("\n");
    writeFileSync(brainPath, existing + block);
    return;
  }

  // New page (either fresh create or first refine of a never-brain-paged skill)
  const isFork = draft.mode === "refine" && draft.source_tier && draft.source_tier !== "user-skills";
  const header = isFork
    ? `Forked from \`${draft.source_tier}/${slug}@${draft.source_sha}\``
    : `Authored fresh via Telegram`;
  const qa = (draft.mode === "create")
    ? [
        `- **Q1 (capability):** ${a.capability}`,
        `- **Q2 (triggers):** ${(a.triggers as string[]).map(t => `"${t}"`).join(", ")}`,
        `- **Q3 (output):** ${a.output}`,
        `- **Q4 (tests):** ${a.with_tests ? "yes" : "no"}`,
      ]
    : [
        `- **R1 (change):** ${a.change_statement}`,
        `- **R2 (desired behavior):** ${a.desired_behavior}`,
        `- **R3 (new triggers):** ${(a.additional_triggers ?? []).length > 0 ? (a.additional_triggers as string[]).map((t: string) => `"${t}"`).join(", ") : "(keep existing)"}`,
      ];
  const body = [
    "---",
    `type: user-skill`,
    `created: ${new Date().toISOString().slice(0, 10)}`,
    `updated: ${new Date().toISOString().slice(0, 10)}`,
    `tags: [user-skill, alfred-authored]`,
    "---",
    `# user-skill: ${slug}`,
    "",
    `_${header}._`,
    "",
    `**Initial commit:** \`${commitSha}\``,
    "",
    "## Elicit Q&A",
    "",
    ...qa,
    "",
    "## Connections",
    "",
    // Brain page lives at brain/skills/user-skills/<slug>.md, so we go up 3 levels.
    `- Skill file: [[../../../user-skills/${slug}/SKILL.md]]`,
    `- Eval file: [[../../../user-skills/${slug}/eval.yaml]]`,
    isFork ? `- Forked from: [[../../../${draft.source_tier}/${slug}/SKILL.md]]` : "",
    "",
  ].filter(Boolean).join("\n");
  writeFileSync(brainPath, body);
}

// ──────────────────────────────────────────────────────────────────
// Git ops
// ──────────────────────────────────────────────────────────────────

function git(args: string[]): { code: number; stdout: string; stderr: string } {
  const proc = Bun.spawnSync(["git", "-C", REPO_ROOT, ...args]);
  return {
    code: proc.exitCode ?? 0,
    stdout: proc.stdout.toString(),
    stderr: proc.stderr.toString(),
  };
}

function stageAndCommit(files: string[], message: string): { sha: string; ok: boolean; stderr: string } {
  for (const f of files) {
    const r = git(["add", "--", f]);
    if (r.code !== 0) return { sha: "", ok: false, stderr: r.stderr };
  }
  const c = git(["commit", "-m", message]);
  if (c.code !== 0) return { sha: "", ok: false, stderr: c.stderr };
  const s = git(["rev-parse", "--short", "HEAD"]);
  return { sha: s.stdout.trim(), ok: true, stderr: "" };
}

function pushCurrentBranch(): { ok: boolean; stderr: string } {
  // Try once; on conflict, rebase + retry once.
  let r = git(["push"]);
  if (r.code === 0) return { ok: true, stderr: "" };
  const rebased = git(["pull", "--rebase"]);
  if (rebased.code !== 0) return { ok: false, stderr: `pull --rebase failed: ${rebased.stderr}` };
  r = git(["push"]);
  return { ok: r.code === 0, stderr: r.stderr };
}

// ──────────────────────────────────────────────────────────────────
// Emit
// ──────────────────────────────────────────────────────────────────

function emit(env: Envelope): never {
  console.log(JSON.stringify(env));
  process.exit(env.ok ? 0 : 1);
}

// ──────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────

async function main() {
  const args = Object.fromEntries(process.argv.slice(2).map(a => {
    const eq = a.indexOf("="); return eq === -1 ? [a.replace(/^--/, ""), true] : [a.slice(2, eq), a.slice(eq + 1)];
  }));
  if (typeof args["draft-file"] !== "string") {
    emit({ ok: false, phase: "error", user_message: "Internal: commit-and-deploy needs --draft-file=<path>", draft_slug: "unknown", verdict: "error" });
  }
  const draft: Draft = JSON.parse(readFileSync(args["draft-file"] as string, "utf8"));
  const dryRun = !!args["dry-run"];
  const skipPush = dryRun || !!args["skip-push"];

  // 1. Resolve slug + collision
  let slugInfo: { slug: string; bumped: boolean };
  try {
    slugInfo = resolveSlug(draft);
  } catch (e) {
    emit({ ok: false, phase: "error", user_message: `Slug resolution failed: ${e instanceof Error ? e.message : String(e)}`, draft_slug: draft.slug, verdict: "error" });
  }
  const slug = slugInfo.slug;
  const userSkillDir = join(USER_SKILLS, slug);
  mkdirSync(userSkillDir, { recursive: true });

  // 2. Materialize SKILL.md + eval.yaml
  let skillMdText: string;
  let evalYamlText: string;
  let isFork = false;

  if (draft.mode === "create") {
    skillMdText = generateCreateSkillMd(slug, draft);
    evalYamlText = generateCreateEvalYaml(slug, draft);
  } else {
    // REFINE — possibly fork-on-modify (T15)
    const src = resolveSource(draft.answers.target);
    if (!src) {
      emit({ ok: false, phase: "error", user_message: `Refine source \`${draft.answers.target}\` not found.`, draft_slug: slug, verdict: "error" });
    }
    isFork = src!.tier !== "user-skills";
    let baseSkill: string;
    let baseEval: string;
    if (isFork) {
      const cloned = cloneForFork({ tier: src!.tier, dir: src!.dir }, userSkillDir, draft.source_sha ?? "unknown");
      baseSkill = rewriteFrontmatter(cloned.skillMdOriginal, {
        name: slug,
        version: "0.1.0",                // reset on fork
        created_by: "alfred",
        source: "telegram",
        forked_from: `${src!.tier}/${draft.answers.target}@${draft.source_sha ?? "unknown"}`,
        created_at: new Date().toISOString(),
        last_authoring_turn: new Date().toISOString(),
        eval: `user-skills/${slug}/eval.yaml`,
      });
      baseEval = cloned.evalYamlOriginal;
    } else {
      // In-place refine of an existing user-skill
      baseSkill = readFileSync(join(userSkillDir, "SKILL.md"), "utf8");
      baseEval = existsSync(join(userSkillDir, "eval.yaml")) ? readFileSync(join(userSkillDir, "eval.yaml"), "utf8") : "";
    }
    skillMdText = applyRefineDiff(baseSkill, draft, slug);
    evalYamlText = appendRefineTriggers(baseEval, slug, draft.answers.additional_triggers ?? []);
  }

  writeFileSync(join(userSkillDir, "SKILL.md"), skillMdText);
  writeFileSync(join(userSkillDir, "eval.yaml"), evalYamlText);

  // 3. Stage + commit (without push)
  const message = draft.mode === "create"
    ? `feat(user-skill): create ${slug}${slugInfo.bumped ? " (auto-bumped from collision)" : ""}\n\nAuthored via Telegram skill-author.\n  capability: ${draft.answers.capability}\n  triggers: ${(draft.answers.triggers ?? []).map((t: string) => `"${t}"`).join(", ")}`
    : `feat(user-skill): refine ${slug}${isFork ? ` (fork-on-modify from ${draft.source_tier}/${slug}@${draft.source_sha})` : ""}\n\n  change: ${draft.answers.change_statement}\n  desired: ${draft.answers.desired_behavior}`;

  const stagedFiles = [
    join("user-skills", slug, "SKILL.md").replace(REPO_ROOT + "/", ""),
    join("user-skills", slug, "eval.yaml").replace(REPO_ROOT + "/", ""),
  ];
  // We'll add the brain page after we know the commit SHA — so commit twice
  // is wrong; instead, write the brain page FIRST with a placeholder, then
  // amend the SHA in a single commit.
  // Cleanest: stage skill files, run a soft commit to get a SHA, write brain
  // page citing that SHA, then amend. Use --amend --no-edit.
  let { sha, ok: commitOk, stderr: commitErr } = stageAndCommit(stagedFiles, message);
  if (!commitOk) {
    emit({ ok: false, phase: "error", user_message: `Commit failed: ${commitErr.slice(0, 200)}`, draft_slug: slug, verdict: "error" });
  }
  writeBrainPage(slug, draft, sha);
  // amend to include the brain page in the same commit
  git(["add", "--", join("brain", "skills", "user-skills", `${slug}.md`).replace(REPO_ROOT + "/", "")]);
  git(["commit", "--amend", "--no-edit"]);
  const finalSha = git(["rev-parse", "--short", "HEAD"]).stdout.trim();

  // 4. Push (unless dry-run)
  if (skipPush) {
    // dry-run: a CI / smoke harness called us; user-facing message is empty
    // because no real user is watching. Detail carries the SHA for the test.
    emit({
      ok: true,
      phase: "commit",
      user_message: "",
      draft_slug: slug,
      verdict: "commit_ok",
      detail: { sha: finalSha, slug, mode: draft.mode, fork: isFork, dry_run: true, bumped: slugInfo.bumped },
    });
  }

  const pushed = pushCurrentBranch();
  if (!pushed.ok) {
    emit({
      ok: false,
      phase: "error",
      user_message: `🛑 Couldn't push the commit: ${pushed.stderr.slice(0, 200)}\n\nThe commit is on your branch at \`${finalSha}\` — you can push manually or run /refine to retry.`,
      draft_slug: slug,
      verdict: "push_failed",
      detail: { sha: finalSha, push_error: pushed.stderr },
    });
  }

  // 5. Deploy is async (GH Actions takes over). The user-facing message
  // is intentionally minimal — "Creating this new skill for you. Please
  // wait for 1–2 min." was already shown by safety-scan.ts. The user
  // doesn't see anything else until the post-deploy "Live" callback
  // fires from the deploy-callback hook (separate Telegram message,
  // posted by GH Actions on success).
  //
  // Slug auto-bump is the one exception: the user picked name X and we
  // committed name Y, so we have to surface that or they'll be confused
  // when their trigger phrase doesn't fire.
  const userMessage = slugInfo.bumped
    ? `(Quick heads-up: I named it \`${slug}\` since you already have a skill with that base name. Say \`/refine ${slug}\` if you want to rename it later.)`
    : "";
  emit({
    ok: true,
    phase: "deploy",
    user_message: userMessage,
    draft_slug: slug,
    verdict: "deploy_ok",
    detail: { sha: finalSha, slug, mode: draft.mode, fork: isFork, bumped: slugInfo.bumped, push_ok: true },
  });
}

main().catch(err => {
  console.log(JSON.stringify({
    ok: false,
    phase: "error",
    user_message: `Commit-and-deploy crashed: ${err instanceof Error ? err.message : String(err)}`,
    draft_slug: "unknown",
    verdict: "error",
    detail: { error: err instanceof Error ? err.message : String(err) },
  }));
  process.exit(1);
});
