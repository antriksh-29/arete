#!/usr/bin/env node
// dry-audit.mjs — flags trigger-phrase overlaps across skills.
//
// Reads every skills/*/SKILL.md frontmatter `trigger:` block, extracts
// quoted phrases, and builds an inverted index. Two failure modes:
//
//   1. EXACT  — same phrase appears in ≥2 skills (hard fail).
//   2. SUBSTR — phrase A is a strict substring of phrase B (warn) — the
//               resolver may pick the wrong one if user types A.
//
// Also reports orphan skills (no triggers parsed) as WARN.
//
// Exit 0 iff no exact collisions. Substring overlaps are WARN.
// Usage: node infra/dry-audit.mjs

import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..');
const SKILLS_DIR = resolve(REPO_ROOT, 'skills');

function readFrontmatter(skillMd) {
  const t = readFileSync(skillMd, 'utf-8');
  if (!t.startsWith('---\n')) return '';
  const end = t.indexOf('\n---\n', 4);
  return end === -1 ? '' : t.slice(4, end);
}

function readTriggerBlock(skillMd) {
  const fm = readFrontmatter(skillMd);
  if (!fm) return null;
  // Capture everything after `trigger: |` up to the next top-level field or
  // end of frontmatter. A top-level field is a line beginning at column 0
  // with `letter:`. (Indented lines inside the block start with two spaces.)
  const m = fm.match(/^trigger:\s*\|-?\s*\n([\s\S]*?)(?=^[a-z_][a-z0-9_]*:|\Z)/m);
  if (!m) return null;
  return m[1].split('\n').map((l) => l.replace(/^  /, '')).join('\n');
}

function readTriggerGuard(skillMd) {
  const fm = readFrontmatter(skillMd);
  if (!fm) return null;
  const m = fm.match(/^trigger_guard:\s*([a-z_][a-z0-9_]*)\s*$/m);
  return m ? m[1] : null;
}

function isConstraintSkill(skillMd) {
  const fm = readFrontmatter(skillMd);
  return /^constraint_skill:\s*true\s*$/m.test(fm);
}

// Modality cascade. Skills sharing a trigger phrase are NOT a hard
// collision when their guards are distinct members of this set —
// the matcher disambiguates by inspecting the actual input shape
// (image attached, URL pattern, etc.).
const MODALITY_GUARDS = new Set([
  'image_attached', 'pdf_url', 'x_url', 'youtube_url', 'any_url',
]);

function extractPhrases(block) {
  // Pull every double-quoted substring. These are the resolver-matchable
  // phrases. Free prose between them is descriptive context, not a phrase.
  if (!block) return [];
  const phrases = [];
  const re = /"([^"]+)"/g;
  let m;
  while ((m = re.exec(block)) !== null) {
    phrases.push(m[1].toLowerCase().trim());
  }
  return phrases;
}

const skillDirs = readdirSync(SKILLS_DIR).filter((n) => {
  const p = resolve(SKILLS_DIR, n);
  return statSync(p).isDirectory() && !n.startsWith('.');
});

const phrasesBySkill = {};   // skill -> [phrase, ...]
const skillsByPhrase = {};   // phrase -> [skill, ...]
const guardBySkill = {};     // skill -> guard | null

for (const skill of skillDirs) {
  const md = resolve(SKILLS_DIR, skill, 'SKILL.md');
  if (!existsSync(md)) continue;
  const block = readTriggerBlock(md);
  const phrases = extractPhrases(block);
  phrasesBySkill[skill] = phrases;
  guardBySkill[skill] = readTriggerGuard(md);
  for (const p of phrases) {
    skillsByPhrase[p] = skillsByPhrase[p] || [];
    if (!skillsByPhrase[p].includes(skill)) skillsByPhrase[p].push(skill);
  }
}

// Classify each shared phrase:
//   - hard FAIL  → ≥2 skills, ANY two share a guard or any has no guard
//   - guarded WARN → ≥2 skills with distinct, recognised modality guards
function classifyCollision(skills) {
  const guards = skills.map((s) => guardBySkill[s]);
  if (guards.some((g) => g === null)) return 'fail';
  if (guards.some((g) => !MODALITY_GUARDS.has(g))) return 'fail';
  if (new Set(guards).size !== guards.length) return 'fail';   // duplicates
  return 'guarded';
}

const exactCollisions = [];
const guardedCollisions = [];
for (const [phrase, ss] of Object.entries(skillsByPhrase)) {
  if (ss.length < 2) continue;
  if (classifyCollision(ss) === 'guarded') guardedCollisions.push([phrase, ss]);
  else exactCollisions.push([phrase, ss]);
}

// SUBSTRING ambiguities — for every (A, B) where A != B and A ⊂ B, report,
// UNLESS the claiming skills disambiguate by distinct modality guards
// (same logic as classifyCollision).
function pairIsGuardDisambiguated(skillsA, skillsB) {
  const all = [...new Set([...skillsA, ...skillsB])];
  const guards = all.map((s) => guardBySkill[s]);
  if (guards.some((g) => g === null)) return false;
  if (guards.some((g) => !MODALITY_GUARDS.has(g))) return false;
  return new Set(guards).size === guards.length;
}

const substr = [];
const allPhrases = Object.keys(skillsByPhrase);
for (const a of allPhrases) {
  for (const b of allPhrases) {
    if (a === b) continue;
    if (a.length < 4) continue;
    if (b.includes(a)) {
      const ssa = skillsByPhrase[a];
      const ssb = skillsByPhrase[b];
      const sa = ssa.join(',');
      const sb = ssb.join(',');
      if (sa === sb) continue;
      if (pairIsGuardDisambiguated(ssa, ssb)) continue;
      substr.push({ short: a, long: b, shortSkill: sa, longSkill: sb });
    }
  }
}

// Orphans — skills with no parseable triggers. Constraint skills (e.g.
// reply-discipline) are not user-fired, so they're expected to have no
// triggers — exclude them from this check.
const orphans = skillDirs.filter((s) => {
  const md = resolve(SKILLS_DIR, s, 'SKILL.md');
  if (!existsSync(md)) return false;
  if (isConstraintSkill(md)) return false;
  return !phrasesBySkill[s] || phrasesBySkill[s].length === 0;
});

// ── report ──────────────────────────────────────────────────────

console.log('## Per-skill trigger counts');
for (const s of skillDirs) {
  const n = (phrasesBySkill[s] || []).length;
  console.log(`  ${s.padEnd(20)} ${n} phrase${n === 1 ? '' : 's'}`);
}
console.log('');

if (exactCollisions.length > 0) {
  console.log('## EXACT collisions (fail)');
  for (const [phrase, ss] of exactCollisions) {
    console.log(`  ✗ "${phrase}" claimed by: ${ss.join(', ')}`);
  }
  console.log('');
}

if (guardedCollisions.length > 0) {
  console.log('## Guarded collisions (warn — disambiguated by modality)');
  for (const [phrase, ss] of guardedCollisions) {
    const labels = ss.map((s) => `${s} [${guardBySkill[s]}]`).join(', ');
    console.log(`  ! "${phrase}" claimed by: ${labels}`);
  }
  console.log('');
}

if (substr.length > 0) {
  console.log('## Substring ambiguities (warn)');
  for (const x of substr) {
    console.log(`  ! "${x.short}" (${x.shortSkill}) is a substring of "${x.long}" (${x.longSkill})`);
  }
  console.log('');
}

if (orphans.length > 0) {
  console.log('## Orphans — no parseable triggers (warn)');
  for (const o of orphans) console.log(`  ! ${o}`);
  console.log('');
}

const failCount = exactCollisions.length;
const warnCount = substr.length + orphans.length + guardedCollisions.length;
console.log(`${failCount} FAIL · ${warnCount} WARN`);
process.exit(failCount > 0 ? 1 : 0);
