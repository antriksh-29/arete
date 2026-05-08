#!/usr/bin/env node
// check-resolvable.mjs — audits every custom skill in this repo for
// resolvability: SKILL.md present, frontmatter shape valid, script
// exists, eval pointer exists, brain dirs match the canonical brain.
//
// Does NOT touch the gbrain-managed block in identity/AGENTS.md
// (lines between <!-- gbrain:skillpack:begin --> and :end -->) — that's
// gbrain's `skillpack-check` responsibility.
//
// Exit 0 iff every custom skill is fully resolvable.
// Exit 1 if any FAIL. WARN entries don't fail the build (yet).
//
// Usage: node infra/check-resolvable.mjs

import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { brainLs, brainExists } from './brain-fetch.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..');
const SKILLS_DIR = resolve(REPO_ROOT, 'skills');
const AGENTS_MD = resolve(REPO_ROOT, 'identity/AGENTS.md');

const REQUIRED_FIELDS = [
  'name', 'version', 'script', 'mutating', 'cron', 'eval',
  'last_audited', 'description', 'trigger', 'writes_to', 'brain_dirs_read',
];

// ── tiny YAML frontmatter parser ────────────────────────────────
// Handles: scalars, multi-line strings (|), flow lists ([]), block lists (-),
// nulls, bools, ints, dates. Enough for our SKILL.md needs.
function parseFrontmatter(md) {
  if (!md.startsWith('---\n')) throw new Error('no frontmatter');
  const end = md.indexOf('\n---\n', 4);
  if (end === -1) throw new Error('unterminated frontmatter');
  const body = md.slice(4, end);
  const out = {};
  const lines = body.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith('#')) { i++; continue; }
    const m = line.match(/^([a-z_][a-z0-9_]*):\s*(.*)$/i);
    if (!m) { i++; continue; }
    const [, key, rest] = m;
    if (rest === '|' || rest === '|-' || rest === '>-' || rest === '>') {
      // multiline scalar — collect indented lines
      const buf = [];
      i++;
      while (i < lines.length && (lines[i].startsWith('  ') || lines[i] === '')) {
        buf.push(lines[i].replace(/^  /, ''));
        i++;
      }
      out[key] = buf.join('\n').replace(/\n+$/, '');
      continue;
    }
    if (rest === '' || rest === '[]') {
      // block list (next indented '- foo' lines) OR empty list
      if (rest === '[]') { out[key] = []; i++; continue; }
      const arr = [];
      i++;
      while (i < lines.length && lines[i].match(/^  -\s+/)) {
        arr.push(lines[i].replace(/^  -\s+/, '').trim());
        i++;
      }
      out[key] = arr;
      continue;
    }
    out[key] = parseScalar(rest);
    i++;
  }
  return out;
}

function parseScalar(v) {
  v = v.trim();
  // Strip trailing inline comment ( ` # ...` — preceded by whitespace, not
  // inside a quoted string).
  if (!v.startsWith('"') && !v.startsWith("'")) {
    const hash = v.indexOf(' #');
    if (hash > -1) v = v.slice(0, hash).trim();
  }
  if (v === 'null' || v === '~' || v === '') return null;
  if (v === 'true') return true;
  if (v === 'false') return false;
  if (/^-?\d+$/.test(v)) return parseInt(v, 10);
  if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
    return v.slice(1, -1);
  }
  return v;
}

// ── audit ───────────────────────────────────────────────────────

const findings = [];  // {skill, level: 'OK'|'FAIL'|'WARN', message}

function record(skill, level, message) {
  findings.push({ skill, level, message });
}

function auditSkill(name) {
  const dir = resolve(SKILLS_DIR, name);
  const skillMd = resolve(dir, 'SKILL.md');

  if (!existsSync(skillMd)) {
    record(name, 'FAIL', 'missing SKILL.md');
    return;
  }

  let fm;
  try {
    fm = parseFrontmatter(readFileSync(skillMd, 'utf-8'));
  } catch (e) {
    record(name, 'FAIL', `frontmatter parse error: ${e.message}`);
    return;
  }

  // Required-field presence
  for (const f of REQUIRED_FIELDS) {
    if (!(f in fm)) record(name, 'FAIL', `frontmatter missing field: ${f}`);
  }

  // name matches dir
  if (fm.name && fm.name !== name) {
    record(name, 'FAIL', `name "${fm.name}" != dir "${name}"`);
  }

  // script exists
  if (fm.script) {
    const scriptPath = resolve(REPO_ROOT, fm.script);
    if (!existsSync(scriptPath)) {
      record(name, 'FAIL', `script not found: ${fm.script}`);
    }
  }

  // helper_scripts (optional)
  if (Array.isArray(fm.helper_scripts)) {
    for (const h of fm.helper_scripts) {
      if (!existsSync(resolve(REPO_ROOT, h))) {
        record(name, 'FAIL', `helper_script not found: ${h}`);
      }
    }
  }

  // eval pointer exists (file may be empty/skeleton in Wave 1 — WARN not FAIL
  // for skills other than the pilot).
  if (fm.eval) {
    const evalPath = resolve(REPO_ROOT, fm.eval);
    if (!existsSync(evalPath)) {
      record(name, 'WARN', `eval file not yet created: ${fm.eval}`);
    }
  }

  // mutating + writes_to consistency
  // A mutating skill must declare its mutation surface — either brain dirs
  // (writes_to) or external state (mutates_external_state: true).
  if (fm.mutating === true) {
    const writesBrain = Array.isArray(fm.writes_to) && fm.writes_to.length > 0;
    const mutatesExt = fm.mutates_external_state === true;
    if (!writesBrain && !mutatesExt) {
      record(name, 'FAIL', 'mutating: true but no writes_to entries and no mutates_external_state: true');
    }
  }
  if (fm.mutating === false && Array.isArray(fm.writes_to) && fm.writes_to.length > 0) {
    record(name, 'FAIL', 'mutating: false but writes_to is non-empty');
  }

  // brain dirs are real
  for (const dir of [...(fm.writes_to || []), ...(fm.brain_dirs_read || [])]) {
    // Strip trailing slash, take first segment for dir-level check
    const seg = dir.replace(/\/$/, '').split('/')[0];
    if (!seg) continue;
    // Allow paths like "lists/to-read" (file) — only check the directory
    // exists in brain. Skip "identity/" — that's repo-local, not brain.
    if (seg === 'identity') continue;
    try {
      if (!brainExists(seg)) {
        record(name, 'WARN', `brain dir not found: ${seg} (referenced in writes_to/brain_dirs_read)`);
      }
    } catch (e) {
      record(name, 'WARN', `could not verify brain dir ${seg}: ${e.message}`);
    }
  }

  // happy path
  if (!findings.some((f) => f.skill === name && f.level === 'FAIL')) {
    record(name, 'OK', '');
  }
}

function checkAgentsCustomBlock() {
  // Sanity check: AGENTS.md exists, gbrain managed block is intact.
  if (!existsSync(AGENTS_MD)) {
    record('_agents', 'FAIL', 'identity/AGENTS.md missing');
    return;
  }
  const t = readFileSync(AGENTS_MD, 'utf-8');
  if (!t.includes('<!-- gbrain:skillpack:begin -->') || !t.includes('<!-- gbrain:skillpack:end -->')) {
    record('_agents', 'WARN', 'gbrain managed block markers not both found in AGENTS.md');
  }
}

// ── run ─────────────────────────────────────────────────────────

const skillDirs = readdirSync(SKILLS_DIR).filter((n) => {
  const p = resolve(SKILLS_DIR, n);
  return statSync(p).isDirectory() && !n.startsWith('.');
});

for (const name of skillDirs) auditSkill(name);
checkAgentsCustomBlock();

// ── report ──────────────────────────────────────────────────────

const byLevel = { OK: 0, WARN: 0, FAIL: 0 };
const lines = [];

for (const name of skillDirs) {
  const f = findings.filter((x) => x.skill === name);
  const fails = f.filter((x) => x.level === 'FAIL');
  const warns = f.filter((x) => x.level === 'WARN');
  if (fails.length === 0 && warns.length === 0) {
    lines.push(`✓ ${name.padEnd(20)} SKILL.md script eval brain-dirs`);
    byLevel.OK++;
  } else {
    if (fails.length) byLevel.FAIL++;
    else byLevel.WARN++;
    const tag = fails.length ? '✗' : '!';
    const issues = [...fails, ...warns].map((x) => `${x.level}: ${x.message}`).join('; ');
    lines.push(`${tag} ${name.padEnd(20)} ${issues}`);
  }
}

const agentFails = findings.filter((x) => x.skill === '_agents');
for (const a of agentFails) {
  lines.push(`${a.level === 'FAIL' ? '✗' : '!'} _agents.md           ${a.level}: ${a.message}`);
  if (a.level === 'FAIL') byLevel.FAIL++;
}

console.log(lines.join('\n'));
console.log('');
console.log(`${byLevel.OK} OK · ${byLevel.WARN} WARN · ${byLevel.FAIL} FAIL`);

process.exit(byLevel.FAIL > 0 ? 1 : 0);
