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

function readTriggerBlock(skillMd) {
  const t = readFileSync(skillMd, 'utf-8');
  if (!t.startsWith('---\n')) return null;
  const end = t.indexOf('\n---\n', 4);
  if (end === -1) return null;
  const fm = t.slice(4, end);
  // Match "trigger: |" or "trigger: |-" or "trigger:" with following indented block
  // Capture everything after `trigger: |` up to the next top-level field or
  // end of frontmatter. A top-level field is a line beginning at column 0
  // with `letter:`. (Indented lines inside the block start with two spaces.)
  const m = fm.match(/^trigger:\s*\|-?\s*\n([\s\S]*?)(?=^[a-z_][a-z0-9_]*:|\Z)/m);
  if (!m) return null;
  // De-indent (drop the leading two spaces from each line)
  return m[1].split('\n').map((l) => l.replace(/^  /, '')).join('\n');
}

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

for (const skill of skillDirs) {
  const md = resolve(SKILLS_DIR, skill, 'SKILL.md');
  if (!existsSync(md)) continue;
  const block = readTriggerBlock(md);
  const phrases = extractPhrases(block);
  phrasesBySkill[skill] = phrases;
  for (const p of phrases) {
    skillsByPhrase[p] = skillsByPhrase[p] || [];
    if (!skillsByPhrase[p].includes(skill)) skillsByPhrase[p].push(skill);
  }
}

// EXACT collisions
const exactCollisions = Object.entries(skillsByPhrase).filter(([, ss]) => ss.length >= 2);

// SUBSTRING ambiguities — for every (A, B) where A != B and A ⊂ B, report.
const substr = [];
const allPhrases = Object.keys(skillsByPhrase);
for (const a of allPhrases) {
  for (const b of allPhrases) {
    if (a === b) continue;
    if (a.length < 4) continue;       // ignore tiny phrases like "X"
    if (b.includes(a)) {
      const sa = skillsByPhrase[a].join(',');
      const sb = skillsByPhrase[b].join(',');
      if (sa !== sb) substr.push({ short: a, long: b, shortSkill: sa, longSkill: sb });
    }
  }
}

// Orphans
const orphans = skillDirs.filter((s) => !phrasesBySkill[s] || phrasesBySkill[s].length === 0);

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
const warnCount = substr.length + orphans.length;
console.log(`${failCount} FAIL · ${warnCount} WARN`);
process.exit(failCount > 0 ? 1 : 0);
