#!/usr/bin/env node
// run-skill-eval.mjs — runs one skill's eval.yaml.
//
// Three layers, gated by flags + env:
//   1. Smoke test       — always (cheap, no network, no LLM)
//   2. Output structural — always (run cmd, assert json keys + expect_ok)
//   3. Trigger routing  — if OPENAI_API_KEY set; gpt-4o-mini scores each
//                         utterance against the resolver table
//   4. Output judge     — opt-in via --judge; gpt-5.4-nano scores each
//                         output's actual stdout against its judge_rubric.
//                         Pass threshold: 8/10.
//
// Pass thresholds (per eval/schemas/skill-eval.schema.md):
//   - Trigger routing: ≥ 90% top-1 accuracy
//   - Output shape: 100% expect_json_keys present, expect_ok matches
//   - Output judge: ≥ 8/10 average per entry
//   - Smoke: exit 0
//
// Exit 0 iff all enabled checks pass.
//
// Usage:
//   node eval/run-skill-eval.mjs skills/image-ocr/
//   node eval/run-skill-eval.mjs skills/image-ocr/ --judge      (LLM judge on)
//   node eval/run-skill-eval.mjs skills/image-ocr/ --with-fixtures

import { readFileSync, existsSync } from 'node:fs';
import { execSync, spawnSync } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..');
const AGENTS_MD = resolve(REPO_ROOT, 'identity/AGENTS.md');

const argv = process.argv.slice(2);
const skillDir = argv.find((a) => !a.startsWith('--'));
const withFixtures = argv.includes('--with-fixtures');
const withJudge = argv.includes('--judge');

if (!skillDir) {
  console.error('Usage: run-skill-eval.mjs <skill-dir> [--judge] [--with-fixtures]');
  process.exit(2);
}

const evalPath = resolve(REPO_ROOT, skillDir, 'eval.yaml');
if (!existsSync(evalPath)) {
  console.error(`no eval.yaml at ${evalPath}`);
  process.exit(2);
}

// ── tiny YAML parser tuned for our eval.yaml shape ──────────────
// Handles: scalars, multi-line strings (|), block lists (-), flow lists ([])
// Sufficient for the schema in eval/schemas/skill-eval.schema.md.
function parseYaml(text) {
  const lines = text.split('\n');
  const root = {};
  let i = 0;

  function indent(s) { return s.match(/^( *)/)[1].length; }
  function trimQ(v) {
    v = v.trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      return v.slice(1, -1);
    }
    return v;
  }
  function asScalar(v) {
    if (v === 'null' || v === '~' || v === '') return null;
    if (v === 'true') return true;
    if (v === 'false') return false;
    if (/^-?\d+$/.test(v)) return parseInt(v, 10);
    if (v.startsWith('[') && v.endsWith(']')) {
      const inner = v.slice(1, -1).trim();
      if (!inner) return [];
      return inner.split(',').map((x) => trimQ(x));
    }
    return trimQ(v);
  }

  function parseBlock(parentIndent) {
    const block = {};
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim() || line.trimStart().startsWith('#')) { i++; continue; }
      const ind = indent(line);
      if (ind <= parentIndent) return block;
      const m = line.match(/^( *)([a-z_][a-z0-9_]*):\s*(.*)$/i);
      if (!m) return block;
      const [, , key, rest] = m;
      // Multi-line scalar (| or |-)
      if (rest === '|' || rest === '|-' || rest === '>' || rest === '>-') {
        const buf = [];
        i++;
        while (i < lines.length && (lines[i].trim() === '' || indent(lines[i]) > ind)) {
          if (lines[i].trim() === '') buf.push('');
          else buf.push(lines[i].slice(ind + 2));
          i++;
        }
        block[key] = buf.join('\n').replace(/\n+$/, '');
        continue;
      }
      // Empty value — could be a block list OR a nested object. Peek
      // ahead at the first non-blank, non-comment line to decide.
      if (rest === '') {
        // Peek past blanks/comments without consuming
        let j = i + 1;
        while (j < lines.length && (!lines[j].trim() || lines[j].trimStart().startsWith('#'))) j++;
        const isList = j < lines.length && lines[j].slice(ind).startsWith('  -');
        if (!isList) {
          // Nested object — recurse with parentIndent = current indent
          i++;
          const nested = parseBlock(ind);
          block[key] = nested;
          continue;
        }
        i++;
        const arr = [];
        // Skip blank/comment lines until we find a list item or a peer field.
        const isItemHere = () => {
          // Skip blanks/comments
          while (i < lines.length && (!lines[i].trim() || lines[i].trimStart().startsWith('#'))) i++;
          if (i >= lines.length) return false;
          if (indent(lines[i]) <= ind) return false;
          return lines[i].slice(ind).startsWith('  -');
        };
        while (isItemHere()) {
          const itemLine = lines[i].slice(ind + 2);    // strip leading "  "
          const dashRest = itemLine.replace(/^- ?/, '');
          if (/^[a-z_][a-z0-9_]*:/.test(dashRest)) {
            // Object item — first key on the dash line, siblings at ind+4
            const obj = {};
            const fm = dashRest.match(/^([a-z_][a-z0-9_]*):\s*(.*)$/);
            if (fm) {
              const [, k0, v0] = fm;
              if (v0 === '|' || v0 === '|-' || v0 === '>' || v0 === '>-') {
                const buf = [];
                i++;
                while (i < lines.length && (lines[i].trim() === '' || indent(lines[i]) > ind + 2)) {
                  if (lines[i].trim() === '') buf.push('');
                  else buf.push(lines[i].slice(ind + 4));
                  i++;
                }
                obj[k0] = buf.join('\n').replace(/\n+$/, '');
              } else {
                obj[k0] = asScalar(v0);
                i++;
              }
            } else {
              i++;
            }
            // Sibling fields at indent ind+4. Skip blanks/comments between them.
            while (true) {
              while (i < lines.length && (!lines[i].trim() || lines[i].trimStart().startsWith('#'))) i++;
              if (i >= lines.length) break;
              if (indent(lines[i]) !== ind + 4) break;
              const fm2 = lines[i].slice(ind + 4).match(/^([a-z_][a-z0-9_]*):\s*(.*)$/);
              if (!fm2) break;
              const [, k, v] = fm2;
              if (v === '|' || v === '|-' || v === '>' || v === '>-') {
                const buf = [];
                i++;
                while (i < lines.length && (lines[i].trim() === '' || indent(lines[i]) > ind + 4)) {
                  if (lines[i].trim() === '') buf.push('');
                  else buf.push(lines[i].slice(ind + 6));
                  i++;
                }
                obj[k] = buf.join('\n').replace(/\n+$/, '');
              } else {
                obj[k] = asScalar(v);
                i++;
              }
            }
            arr.push(obj);
          } else {
            arr.push(asScalar(dashRest));
            i++;
          }
        }
        block[key] = arr;
        continue;
      }
      block[key] = asScalar(rest);
      i++;
    }
    return block;
  }

  const out = parseBlock(-1);
  return out;
}

// ── load + parse the skill eval ─────────────────────────────────
const cfg = parseYaml(readFileSync(evalPath, 'utf-8'));

console.log(`# eval: ${cfg.skill}\n`);

// ── 1. SMOKE ─────────────────────────────────────────────────────
let smokeOk = false;
if (cfg.smoke && cfg.smoke.cmd) {
  console.log('## smoke');
  const r = spawnSync('bash', ['-c', cfg.smoke.cmd], {
    cwd: REPO_ROOT, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
  });
  const exit = r.status;
  const expect = cfg.smoke.expect_exit ?? 0;
  smokeOk = exit === expect;
  console.log(`  cmd:      ${cfg.smoke.cmd}`);
  console.log(`  exit:     ${exit}  (expect ${expect})`);
  if (r.stdout && r.stdout.length < 400) console.log(`  stdout:   ${r.stdout.trim()}`);
  if (r.stderr && r.stderr.length < 400) console.log(`  stderr:   ${r.stderr.trim()}`);
  console.log(`  result:   ${smokeOk ? '✓' : '✗'}\n`);
} else {
  console.log('## smoke\n  (no smoke block — skipping)\n');
  smokeOk = true;
}

// ── 2. TRIGGER ROUTING ──────────────────────────────────────────
let triggerOk = true;
let triggerSkipped = false;
if (cfg.triggers && cfg.triggers.length > 0) {
  console.log('## trigger routing');
  if (!process.env.OPENAI_API_KEY) {
    console.log('  (OPENAI_API_KEY not set — skipping LLM-based routing eval)\n');
    triggerSkipped = true;
  } else {
    const resolverTable = extractResolverTable();
    const results = await runTriggerEvals(cfg.triggers, resolverTable);
    const passed = results.filter((r) => r.pass).length;
    const total = results.length;
    const pct = (passed / total) * 100;
    triggerOk = pct >= 90;
    for (const r of results) {
      const tag = r.pass ? '✓' : '✗';
      console.log(`  ${tag} ${r.id.padEnd(14)} expect=${r.expect.padEnd(18)} got=${r.got.padEnd(18)} "${r.utterance.slice(0, 50)}"`);
    }
    console.log(`  ${passed}/${total} (${pct.toFixed(0)}%)  ${triggerOk ? '✓' : '✗'}\n`);
  }
} else {
  console.log('## trigger routing\n  (no triggers — skipping)\n');
}

// ── 3. OUTPUT EVALS ──────────────────────────────────────────────
// Run each output eval entry: execute cmd, parse stdout as JSON, assert
// expect_json_keys are present and (if specified) expect_ok matches.
// Capture stdout per entry so the judge layer (--judge) can score it.
let outputsOk = true;
let outputsRan = 0;
const judgeCandidates = [];   // {id, rubric, stdout} — only entries that
                              // passed structural checks AND have a rubric
if (Array.isArray(cfg.outputs) && cfg.outputs.length > 0) {
  console.log('## outputs');
  for (const o of cfg.outputs) {
    outputsRan++;
    const cmd = (o.cmd || '').replace('{fixture}', o.fixture || '');
    if (!cmd) { console.log(`  ! ${o.id} — no cmd; skip`); continue; }
    const r = spawnSync('bash', ['-c', cmd], {
      cwd: REPO_ROOT, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'],
    });
    let pass = true;
    let why = '';
    let parsed = null;
    if (r.status !== 0) {
      pass = false; why = `non-zero exit (${r.status})`;
    } else {
      try { parsed = JSON.parse(r.stdout.trim()); }
      catch (e) { pass = false; why = `non-JSON stdout: ${r.stdout.slice(0, 80)}`; }
    }
    if (pass && Array.isArray(o.expect_json_keys)) {
      for (const k of o.expect_json_keys) {
        if (!(k in parsed)) { pass = false; why = `missing key: ${k}`; break; }
      }
    }
    if (pass && 'expect_ok' in o && parsed.ok !== o.expect_ok) {
      pass = false; why = `expect_ok=${o.expect_ok}, got ok=${parsed.ok}`;
    }
    const tag = pass ? '✓' : '✗';
    if (pass) console.log(`  ${tag} ${o.id}`);
    else { console.log(`  ${tag} ${o.id}  (${why})`); outputsOk = false; }
    // Queue for judge if structural pass AND rubric present
    if (pass && o.judge_rubric) {
      judgeCandidates.push({ id: o.id, rubric: o.judge_rubric, stdout: r.stdout });
    }
  }
  console.log(`  ${outputsRan} ran  ${outputsOk ? '✓' : '✗'}\n`);
} else {
  console.log('## outputs\n  (no outputs block — skipping)\n');
}

// ── 4. OUTPUT JUDGE (--judge) ────────────────────────────────────
// LLM-as-judge on each output entry's actual stdout against its
// judge_rubric. Uses gpt-5.4-nano (cheapest tier; ~30 calls per
// full --judge run is well under $0.05). Pass threshold: 8/10.
let judgeOk = true;
let judgeSkipped = false;
if (judgeCandidates.length > 0) {
  console.log('## output judge');
  if (!withJudge) {
    console.log(`  (--judge not set — skipping ${judgeCandidates.length} rubric-eligible entries)\n`);
    judgeSkipped = true;
  } else if (!process.env.OPENAI_API_KEY) {
    console.log('  (OPENAI_API_KEY not set — skipping)\n');
    judgeSkipped = true;
  } else {
    let passed = 0;
    for (const c of judgeCandidates) {
      const verdict = await runJudge(c.rubric, c.stdout);
      const tag = verdict.score >= 8 ? '✓' : '✗';
      if (verdict.score >= 8) passed++;
      else judgeOk = false;
      console.log(`  ${tag} ${c.id.padEnd(20)} ${verdict.score}/10  ${verdict.rationale.slice(0, 80)}`);
    }
    console.log(`  ${passed}/${judgeCandidates.length}  ${judgeOk ? '✓' : '✗'}\n`);
  }
}

if (withFixtures) {
  // Reserved for future LLM-judge / fixture-driven evals.
}

// ── verdict ─────────────────────────────────────────────────────
const overall = smokeOk
  && (triggerOk || triggerSkipped)
  && outputsOk
  && (judgeOk || judgeSkipped);
console.log(`overall: ${overall ? '✓' : '✗'}`);
process.exit(overall ? 0 : 1);


// ── helpers ─────────────────────────────────────────────────────

function extractResolverTable() {
  const md = readFileSync(AGENTS_MD, 'utf-8');
  // Pull the gbrain managed block + any custom triggers above it
  const begin = md.indexOf('<!-- gbrain:skillpack:begin -->');
  const end = md.indexOf('<!-- gbrain:skillpack:end -->');
  let table = '';
  if (begin !== -1 && end !== -1) table = md.slice(begin, end);
  // Also include the per-skill descriptions from each SKILL.md (cheap context)
  // — read the description: field of every skills/*/SKILL.md
  const skillsDir = resolve(REPO_ROOT, 'skills');
  const dirs = readdirSyncSafe(skillsDir);
  const descLines = [];
  for (const d of dirs) {
    const skMd = resolve(skillsDir, d, 'SKILL.md');
    if (!existsSync(skMd)) continue;
    const txt = readFileSync(skMd, 'utf-8');
    const m = txt.match(/^description:\s*\|\s*\n([\s\S]*?)(?=^[a-z_][a-z0-9_]*:|\n---)/m);
    if (m) descLines.push(`### ${d}\n${m[1].replace(/^  /gm, '')}`);
  }
  return `${table}\n\n## Skill descriptions\n\n${descLines.join('\n\n')}`;
}

function readdirSyncSafe(p) {
  try { return execSync(`ls -1 ${p}`, { encoding: 'utf-8' }).trim().split('\n'); }
  catch { return []; }
}

async function runTriggerEvals(triggers, resolverTable) {
  const results = [];
  for (const t of triggers) {
    const got = await routeUtterance(t, resolverTable);
    results.push({
      id: t.id,
      utterance: t.utterance,
      expect: t.expect_skill,
      got,
      pass: got === t.expect_skill,
    });
  }
  return results;
}

async function routeUtterance(t, resolverTable) {
  const flags = [];
  if (t.has_image) flags.push('IMAGE_ATTACHMENT');
  if (t.has_url) flags.push('URL_PRESENT');
  const flagStr = flags.length ? ` [flags: ${flags.join(', ')}]` : '';

  const sys = `You are the resolver for a personal assistant. Given a user utterance and any input flags (image attachment, URL present), return EXACTLY ONE of the skill names from the resolver table below, or "default" if no skill applies. Reply with the skill name only — no explanation, no punctuation.

${resolverTable}`;

  const user = `Utterance: "${t.utterance}"${flagStr}`;

  const body = {
    model: 'gpt-4o-mini',
    messages: [
      { role: 'system', content: sys },
      { role: 'user', content: user },
    ],
    temperature: 0,
    max_tokens: 20,
  };

  const r = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const errBody = await r.text();
    throw new Error(`openai ${r.status}: ${errBody.slice(0, 200)}`);
  }
  const j = await r.json();
  const out = (j.choices?.[0]?.message?.content || '').trim().toLowerCase();
  // Normalize: strip backticks, quotes, periods
  return out.replace(/[`"'.]/g, '').trim();
}

// ── LLM-as-judge for output evals ───────────────────────────────
// Scores actual stdout against a written rubric. Returns
// {score: 0-10, verdict: "pass"|"fail", rationale: "..."}.
// Model: gpt-5.4-nano (cheapest tier, fast, fine for binary rubric work).
async function runJudge(rubric, actualStdout) {
  const sys = `You are scoring an automated test output against a written rubric. Reply with ONLY a JSON object on one line, no prose, no code fences:
{"score": <integer 0-10>, "verdict": "pass" | "fail", "rationale": "<one sentence, max 100 chars>"}

Scoring guide:
- 8-10: output meets the rubric's intent fully or near-fully.
- 5-7: output partially meets the rubric (some criteria pass, some don't).
- 0-4: output fails the rubric materially (a hard-fail pattern is present, or the output doesn't address the rubric at all).

Respect "Hard fails:" lines in the rubric — any one of them caps the score at 4.`;

  const user = `Rubric:
${rubric}

Actual output (truncated to 4KB):
${actualStdout.slice(0, 4096)}`;

  const body = {
    model: 'gpt-5.4-nano',
    messages: [
      { role: 'system', content: sys },
      { role: 'user', content: user },
    ],
    temperature: 0,
    max_tokens: 200,
  };

  const r = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const errBody = await r.text();
    return { score: 0, verdict: 'fail', rationale: `judge api error ${r.status}: ${errBody.slice(0, 60)}` };
  }
  const j = await r.json();
  const content = (j.choices?.[0]?.message?.content || '').trim();
  // Strip markdown code fences if present.
  const stripped = content.replace(/^```(?:json)?\s*/i, '').replace(/\s*```\s*$/i, '').trim();
  try {
    const parsed = JSON.parse(stripped);
    const score = Math.max(0, Math.min(10, parseInt(parsed.score, 10) || 0));
    return {
      score,
      verdict: parsed.verdict || (score >= 8 ? 'pass' : 'fail'),
      rationale: (parsed.rationale || '').slice(0, 200),
    };
  } catch (e) {
    return { score: 0, verdict: 'fail', rationale: `judge returned non-JSON: ${stripped.slice(0, 60)}` };
  }
}
