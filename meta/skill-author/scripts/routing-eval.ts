#!/usr/bin/env -S bun run
/**
 * routing-eval.ts — the routing gate.
 *
 * Two responsibilities:
 *   1. Verify a candidate skill's NEW fixtures route to it (not to a neighbor).
 *   2. Verify all EXISTING fixtures across the catalog still route to their
 *      stated skill (no regression caused by the new skill's description).
 *
 * Resolver-query mechanism: shells out to `python3 infra/e2e-direct.py
 * --route-only --message "<utterance>"`. That command returns a JSON line
 * `{ok, skill, elapsed_s}` with the skill name the resolver WOULD pick
 * (instructions tell the agent not to actually execute). This stays in
 * sync with production resolver behavior because it IS the production
 * resolver — just with a system-prompt clamp.
 *
 * Concurrency: 8 parallel workers (configurable). Full sweep across
 * the existing ~185 fixtures targets <30s wall time.
 *
 * Latency budget: this script runs AT COMMIT-GATE TIME ONLY, never on
 * the user-facing chat turn. The user sees a "checking trigger phrases"
 * status update; routing-eval is allowed up to ~30 seconds.
 *
 * Output: single JSON line on stdout, same envelope shape as the rest
 * of the skill-author pipeline.
 *
 * Modes:
 *   --self-test                         load all eval.yaml files under
 *                                       skills/, openclaw/skills/,
 *                                       user-skills/; verify each fixture
 *                                       still routes to its expect_skill.
 *                                       Used as the pre-push regression
 *                                       check.
 *
 *   --candidate=<slug>                  read user-skills/<slug>/eval.yaml,
 *                                       verify its fixtures route to
 *                                       <slug>, plus run the full sweep
 *                                       to catch regressions.
 *
 *   --fork-mode --target=<name>         after fork-on-modify, assert the
 *                                       cloned skill's original triggers
 *                                       now route to user-skills/<name>
 *                                       (NOT skills/<name> or
 *                                       openclaw/skills/<name>).
 */

import { existsSync, readFileSync, readdirSync, statSync } from "fs";
import { join, resolve } from "path";

// ──────────────────────────────────────────────────────────────────
// Types
// ──────────────────────────────────────────────────────────────────

interface Fixture {
  id: string;
  utterance: string;
  expect_skill: string;
  source_file: string;       // path to the eval.yaml it came from
}

interface RouteResult {
  fixture: Fixture;
  routed_skill: string;
  ok: boolean;               // true iff routed_skill === fixture.expect_skill (or, in fork mode, the override target)
  elapsed_s: number;
  error?: string;
}

interface Envelope {
  ok: boolean;
  phase: "routing";
  user_message: string;
  draft_slug: string | null;
  verdict: "pass" | "collision" | "too_vague" | "fork_routing_ok" | "regression" | "error";
  detail?: Record<string, unknown>;
}

// ──────────────────────────────────────────────────────────────────
// Config
// ──────────────────────────────────────────────────────────────────

const REPO_ROOT = process.env.REPO_ROOT ?? resolve(__dirname, "../../..");
const E2E_DIRECT = join(REPO_ROOT, "infra", "e2e-direct.py");
const PARALLELISM = Number(process.env.ROUTING_EVAL_WORKERS ?? 8);
const PER_CALL_TIMEOUT = 30;       // seconds; matches e2e-direct default

const SKILL_DIRS = ["skills", "openclaw/skills", "user-skills"] as const;

// ──────────────────────────────────────────────────────────────────
// Fixture loader (lightweight YAML — same shape skillify uses)
// ──────────────────────────────────────────────────────────────────

function loadFixturesFromYaml(filePath: string): Fixture[] {
  const text = readFileSync(filePath, "utf8");
  const out: Fixture[] = [];
  let cur: Partial<Fixture> | null = null;
  for (const raw of text.split("\n")) {
    const line = raw;
    // New fixture starts at `  - id: <something>`
    const idMatch = line.match(/^\s*-\s*id:\s*"?([^"\n]+?)"?\s*$/);
    if (idMatch) {
      if (cur && cur.id && cur.utterance && cur.expect_skill) {
        out.push({ ...(cur as Fixture), source_file: filePath });
      }
      cur = { id: idMatch[1].trim() };
      continue;
    }
    if (!cur) continue;
    const m = line.match(/^\s*(utterance|expect_skill):\s*"?([^"\n]+?)"?\s*$/);
    if (m) {
      const k = m[1];
      const v = m[2].trim();
      if (k === "utterance") cur.utterance = v;
      else if (k === "expect_skill") cur.expect_skill = v;
    }
  }
  if (cur && cur.id && cur.utterance && cur.expect_skill) {
    out.push({ ...(cur as Fixture), source_file: filePath });
  }
  return out;
}

function loadAllFixtures(): Fixture[] {
  const all: Fixture[] = [];
  for (const tier of SKILL_DIRS) {
    const tierDir = join(REPO_ROOT, tier);
    if (!existsSync(tierDir)) continue;
    for (const name of readdirSync(tierDir)) {
      const evalPath = join(tierDir, name, "eval.yaml");
      if (!existsSync(evalPath)) continue;
      if (!statSync(evalPath).isFile()) continue;
      try {
        all.push(...loadFixturesFromYaml(evalPath));
      } catch (e) {
        process.stderr.write(`[routing-eval] failed to load ${evalPath}: ${e instanceof Error ? e.message : String(e)}\n`);
      }
    }
  }
  return all;
}

// ──────────────────────────────────────────────────────────────────
// Resolver query (shells e2e-direct.py --route-only)
// ──────────────────────────────────────────────────────────────────

async function queryResolver(utterance: string): Promise<{ skill: string; elapsed_s: number; error?: string }> {
  try {
    const proc = Bun.spawn(
      ["python3", E2E_DIRECT, "--route-only", "--message", utterance, "--timeout", String(PER_CALL_TIMEOUT)],
      { stdout: "pipe", stderr: "pipe" },
    );
    const stdout = await new Response(proc.stdout).text();
    const code = await proc.exited;
    const line = stdout.trim().split("\n").at(-1) ?? "";
    try {
      const parsed = JSON.parse(line) as { ok: boolean; skill?: string; elapsed_s?: number; raw_excerpt?: string };
      return {
        skill: parsed.skill ?? "error",
        elapsed_s: parsed.elapsed_s ?? 0,
        error: parsed.ok ? undefined : (parsed.raw_excerpt ?? `e2e-direct exit ${code}`),
      };
    } catch {
      return { skill: "error", elapsed_s: 0, error: `non-JSON: ${line.slice(0, 200)}` };
    }
  } catch (e) {
    return { skill: "error", elapsed_s: 0, error: e instanceof Error ? e.message : String(e) };
  }
}

// ──────────────────────────────────────────────────────────────────
// Worker pool
// ──────────────────────────────────────────────────────────────────

async function runPool(fixtures: Fixture[], expectOverride?: Map<string, string>): Promise<RouteResult[]> {
  const results: RouteResult[] = [];
  let cursor = 0;
  async function worker() {
    while (true) {
      const i = cursor++;
      if (i >= fixtures.length) return;
      const f = fixtures[i];
      const r = await queryResolver(f.utterance);
      // For fork-mode, the expectation may be overridden (e.g. fixture says
      // expect_skill: activity-tracker but post-fork we expect user-skills/activity-tracker).
      const expected = expectOverride?.get(f.id) ?? f.expect_skill;
      const ok = r.skill === expected;
      results[i] = {
        fixture: f,
        routed_skill: r.skill,
        ok,
        elapsed_s: r.elapsed_s,
        error: r.error,
      };
      if (process.env.ROUTING_EVAL_VERBOSE) {
        process.stderr.write(`[${i + 1}/${fixtures.length}] ${ok ? "✓" : "✗"} ${f.id} → ${r.skill} (exp ${expected})\n`);
      }
    }
  }
  await Promise.all(Array.from({ length: PARALLELISM }, worker));
  return results;
}

// ──────────────────────────────────────────────────────────────────
// Mode dispatchers
// ──────────────────────────────────────────────────────────────────

function emit(env: Envelope): never {
  console.log(JSON.stringify(env));
  process.exit(env.ok ? 0 : 1);
}

async function modeSelfTest(): Promise<never> {
  const fixtures = loadAllFixtures();
  process.stderr.write(`[routing-eval] self-test: ${fixtures.length} fixtures across ${SKILL_DIRS.length} tiers\n`);
  const t0 = Date.now();
  const results = await runPool(fixtures);
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  const failures = results.filter(r => !r.ok);
  if (failures.length === 0) {
    emit({
      ok: true,
      phase: "routing",
      user_message: `routing self-test: all ${fixtures.length} fixtures passed (${elapsed}s, ${PARALLELISM} workers)`,
      draft_slug: null,
      verdict: "pass",
      detail: { total: fixtures.length, elapsed_s: Number(elapsed), workers: PARALLELISM },
    });
  }
  emit({
    ok: false,
    phase: "routing",
    user_message: `✗ Routing self-test: ${failures.length}/${fixtures.length} fixtures regressed.\n\nFirst 5 failures:\n${failures.slice(0, 5).map(f => `  • ${f.fixture.id} → ${f.routed_skill} (expected ${f.fixture.expect_skill}) [${f.fixture.source_file.replace(REPO_ROOT + "/", "")}]`).join("\n")}`,
    draft_slug: null,
    verdict: "regression",
    detail: { total: fixtures.length, failed: failures.length, elapsed_s: Number(elapsed), failures: failures.slice(0, 20) },
  });
}

async function modeCandidate(slug: string): Promise<never> {
  // Load the candidate's fixtures + all neighbors. Verify: (a) every
  // candidate fixture routes to <slug>, (b) every neighbor fixture
  // still routes to its expected skill.
  const candidateEvalPath = join(REPO_ROOT, "user-skills", slug, "eval.yaml");
  if (!existsSync(candidateEvalPath)) {
    emit({
      ok: false,
      phase: "routing",
      user_message: `Couldn't find eval.yaml for candidate skill \`${slug}\`. The elicit step should have generated one before reaching the routing gate.`,
      draft_slug: slug,
      verdict: "error",
    });
  }
  const candidateFixtures = loadFixturesFromYaml(candidateEvalPath).filter(f => f.expect_skill === slug);
  const neighbors = loadAllFixtures().filter(f =>
    !(f.source_file.includes(`/user-skills/${slug}/`)),
  );
  const all = [...candidateFixtures, ...neighbors];

  // Special check: candidate fixtures with very short utterances (<3 words)
  // are likely too vague — flag before even running the sweep.
  const tooVague = candidateFixtures.filter(f => f.utterance.trim().split(/\s+/).length < 3);
  if (tooVague.length > 0) {
    emit({
      ok: false,
      phase: "routing",
      user_message: `⚠️ Trigger "${tooVague[0].utterance}" is too short (< 3 words). Single- or two-word triggers fire by accident on neighbor topics. Add specificity — e.g. a distinctive noun or verb.`,
      draft_slug: slug,
      verdict: "too_vague",
      detail: { too_vague_triggers: tooVague.map(f => f.utterance) },
    });
  }

  const t0 = Date.now();
  const results = await runPool(all);
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  const candidateResults = results.filter(r => r.fixture.source_file === candidateEvalPath);
  const neighborResults = results.filter(r => r.fixture.source_file !== candidateEvalPath);

  // Candidate must route to itself
  const candidateMisses = candidateResults.filter(r => !r.ok);
  if (candidateMisses.length > 0) {
    const m = candidateMisses[0];
    emit({
      ok: false,
      phase: "routing",
      user_message: `⚠️ Your trigger "${m.fixture.utterance}" would route to \`${m.routed_skill}\` instead of \`${slug}\`.\n\nRephrase that trigger — try something more specific so the resolver picks your new skill.`,
      draft_slug: slug,
      verdict: "collision",
      detail: {
        colliding_skill: m.routed_skill,
        colliding_trigger: m.fixture.utterance,
        elapsed_s: Number(elapsed),
        all_candidate_results: candidateResults,
      },
    });
  }

  // Neighbors must still route to themselves
  const neighborRegressions = neighborResults.filter(r => !r.ok);
  if (neighborRegressions.length > 0) {
    const r = neighborRegressions[0];
    emit({
      ok: false,
      phase: "routing",
      user_message: `⚠️ Adding \`${slug}\` would steal routing from \`${r.fixture.expect_skill}\`.\n\nNeighbor fixture "${r.fixture.utterance}" now routes to \`${r.routed_skill}\` instead. This usually means your description is too broad. Want to narrow it?`,
      draft_slug: slug,
      verdict: "regression",
      detail: {
        regressed: neighborRegressions.slice(0, 10),
        elapsed_s: Number(elapsed),
      },
    });
  }

  // Internal-only message — the user-facing message was already emitted
  // by safety-scan.ts ("Creating this new skill for you..."). routing-eval
  // is a transparent backend gate; success here means we proceed to
  // commit-and-deploy without surfacing anything.
  emit({
    ok: true,
    phase: "routing",
    user_message: "",   // commit-and-deploy will emit the next user-visible line
    draft_slug: slug,
    verdict: "pass",
    detail: {
      candidate_passed: candidateResults.length,
      neighbors_checked: neighborResults.length,
      elapsed_s: Number(elapsed),
    },
  });
}

async function modeFork(target: string): Promise<never> {
  // After fork-on-modify, the cloned skill lives at user-skills/<target>/.
  // The original at skills/<target>/ or openclaw/skills/<target>/ still
  // exists. The overlay rule (AGENTS.md §User-skills overlay) says
  // user-skills/ wins. We verify that by re-running the SOURCE skill's
  // fixtures and asserting they now route to the user-skills/ copy.
  const userPath = join(REPO_ROOT, "user-skills", target, "eval.yaml");
  if (!existsSync(userPath)) {
    emit({
      ok: false,
      phase: "routing",
      user_message: `Fork verification failed: no eval.yaml at user-skills/${target}/. The fork-on-modify step should have created it.`,
      draft_slug: target,
      verdict: "error",
    });
  }
  // Source eval fixtures may live in skills/ or openclaw/skills/.
  const sourcePaths = [
    join(REPO_ROOT, "skills", target, "eval.yaml"),
    join(REPO_ROOT, "openclaw/skills", target, "eval.yaml"),
  ].filter(existsSync);
  if (sourcePaths.length === 0) {
    emit({
      ok: false,
      phase: "routing",
      user_message: `Fork verification: no source eval.yaml found for \`${target}\` in skills/ or openclaw/skills/. Was it actually a fork-on-modify?`,
      draft_slug: target,
      verdict: "error",
    });
  }
  const sourceFixtures = sourcePaths.flatMap(p => loadFixturesFromYaml(p).filter(f => f.expect_skill === target));
  // After fork, we expect ALL of these to route to `target` still — that's
  // both the original behavior AND the overlay's intent. The overlay's
  // contract is "same name routes to user-skills/<name>", which from the
  // resolver-output perspective is just "<name>". So no override map
  // needed; we just rerun.
  const t0 = Date.now();
  const results = await runPool(sourceFixtures);
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  const misses = results.filter(r => !r.ok);
  if (misses.length > 0) {
    emit({
      ok: false,
      phase: "routing",
      user_message: `⚠️ Fork-routing assertion failed: ${misses.length}/${sourceFixtures.length} source fixtures no longer route to \`${target}\`. The overlay precedence is broken — check identity/AGENTS.md → ## User-skills overlay.`,
      draft_slug: target,
      verdict: "regression",
      detail: { failed: misses, elapsed_s: Number(elapsed) },
    });
  }
  // Backend-only message; commit-and-deploy emits the next user-visible line.
  emit({
    ok: true,
    phase: "routing",
    user_message: "",
    draft_slug: target,
    verdict: "fork_routing_ok",
    detail: { source_fixtures_passing: sourceFixtures.length, elapsed_s: Number(elapsed) },
  });
}

// ──────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────

async function main() {
  const args = Object.fromEntries(process.argv.slice(2).map(a => {
    const eq = a.indexOf("="); return eq === -1 ? [a.replace(/^--/, ""), true] : [a.slice(2, eq), a.slice(eq + 1)];
  }));

  if (args["self-test"]) {
    return modeSelfTest();
  }
  if (args["fork-mode"]) {
    const target = args["target"] as string;
    if (!target) {
      emit({ ok: false, phase: "routing", user_message: "--fork-mode requires --target=<name>", draft_slug: null, verdict: "error" });
    }
    return modeFork(target);
  }
  const slug = args["candidate"] as string;
  if (slug) {
    return modeCandidate(slug);
  }
  emit({
    ok: false,
    phase: "routing",
    user_message: "Usage: routing-eval.ts --self-test | --candidate=<slug> | --fork-mode --target=<name>",
    draft_slug: null,
    verdict: "error",
  });
}

main().catch(err => {
  console.log(JSON.stringify({
    ok: false,
    phase: "routing",
    user_message: `Routing eval crashed: ${err instanceof Error ? err.message : String(err)}`,
    draft_slug: null,
    verdict: "error",
    detail: { error: err instanceof Error ? err.message : String(err) },
  }));
  process.exit(1);
});
