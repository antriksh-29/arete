#!/usr/bin/env node
// Retrieval eval runner. Reads eval/retrieval-eval.yaml, runs each query
// against the live gbrain via `gbrain query --json`, scores nDCG@3, MRR,
// precision@3, recall@10, and prints a pass/fail summary against the
// thresholds in the eng plan (§A1: nDCG@3 ≥ 0.7, MRR ≥ 0.8).
//
// Usage:  node run-retrieval-eval.mjs [eval/retrieval-eval.yaml]
//
// Env: OPENAI_API_KEY required for gbrain query embedding.

import { readFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { resolve } from 'node:path';

const file = process.argv[2] || resolve(import.meta.dirname, 'retrieval-eval.yaml');
const raw = readFileSync(file, 'utf-8');

// Tiny YAML extractor — we only need the array of objects with known fields.
// Avoids pulling in a YAML lib for a one-file runner.
function parseYaml(txt) {
  // Strip leading comment block. Each entry begins with "- id:" at column 0.
  const lines = txt.split('\n').filter(l => !l.startsWith('#'));
  const entries = [];
  let e = null;
  let cur = null;
  for (const line of lines) {
    if (/^- id:/.test(line)) {
      if (e && e.id) entries.push(e);
      e = { expected_top3: [], acceptable_top10: [], entity_boost_for: [] };
      e.id = line.replace(/^- id:\s*/, '').trim();
      cur = null;
      continue;
    }
    if (!e) continue;
    let unquote = (v) => {
      v = v.trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) return v.slice(1, -1);
      return v;
    };
    const scalar = line.match(/^\s+(query|intent|recency_decay|notes):\s*(.*)$/);
    if (scalar) {
      cur = null;
      e[scalar[1]] = unquote(scalar[2]);
      continue;
    }
    const listEmpty = line.match(/^\s+(expected_top3|acceptable_top10|entity_boost_for):\s*\[\]\s*$/);
    if (listEmpty) { cur = null; continue; }
    const listHeader = line.match(/^\s+(expected_top3|acceptable_top10|entity_boost_for):\s*$/);
    if (listHeader) { cur = listHeader[1]; e[cur] = []; continue; }
    const item = line.match(/^\s+-\s+(.+)$/);
    if (cur && item) e[cur].push(unquote(item[1]));
  }
  if (e && e.id) entries.push(e);
  return entries;
}

function gbrainQuery(q) {
  const out = execSync(`PATH=${STATE_HOME}/.bun/bin:$PATH gbrain query ${JSON.stringify(q)} 2>&1`, { encoding: 'utf-8', maxBuffer: 4_000_000 });
  // Output looks like: "[0.8980] companies/armoriq -- # ArmorIQ\n..."
  const hits = [];
  for (const line of out.split('\n')) {
    const m = line.match(/^\[([0-9.]+)\]\s+([^\s]+)\s+--/);
    if (m) hits.push({ score: parseFloat(m[1]), id: m[2] });
  }
  return hits;
}

function score(entry, hits) {
  const top10 = hits.slice(0, 10).map(h => h.id);
  const top3 = top10.slice(0, 3);
  const expected = new Set(entry.expected_top3);
  const acceptable = new Set([...entry.expected_top3, ...entry.acceptable_top10]);

  // precision@3: of the top-3 returned, fraction in expected
  const p3 = top3.filter(id => expected.has(id)).length / 3;
  // recall@10
  const r10 = entry.expected_top3.length === 0
    ? 1
    : top10.filter(id => expected.has(id)).length / entry.expected_top3.length;
  // MRR: 1/rank of first expected
  let mrrRank = 0;
  for (let i = 0; i < top10.length; i++) {
    if (expected.has(top10[i])) { mrrRank = i + 1; break; }
  }
  const mrr = mrrRank ? 1 / mrrRank : 0;
  // nDCG@3 (binary relevance)
  const dcg = top3.reduce((s, id, i) => s + (expected.has(id) ? 1 / Math.log2(i + 2) : 0), 0);
  const idealCount = Math.min(3, entry.expected_top3.length);
  const idcg = Array.from({ length: idealCount }, (_, i) => 1 / Math.log2(i + 2)).reduce((a, b) => a + b, 0);
  const ndcg = idcg ? dcg / idcg : 0;

  return { p3, r10, mrr, ndcg, top3, top10 };
}

const entries = parseYaml(raw);
console.log(`Running ${entries.length} retrieval queries…\n`);

const results = [];
for (const e of entries) {
  process.stdout.write(`[${e.id}] ${e.query} … `);
  let hits;
  try { hits = gbrainQuery(e.query); }
  catch (err) { console.log('ERROR'); console.error(err.message); continue; }
  const s = score(e, hits);
  results.push({ entry: e, ...s });
  const passNdcg = s.ndcg >= 0.7;
  const passMrr = s.mrr >= 0.8;
  const tag = passNdcg && passMrr ? 'PASS' : 'FAIL';
  console.log(`${tag} (nDCG@3=${s.ndcg.toFixed(2)} MRR=${s.mrr.toFixed(2)} P@3=${s.p3.toFixed(2)} R@10=${s.r10.toFixed(2)})`);
  if (!passNdcg || !passMrr) {
    console.log(`  expected: ${e.expected_top3.join(', ')}`);
    console.log(`  got:      ${s.top3.join(', ')}`);
  }
}

const avg = (arr, k) => arr.reduce((s, r) => s + r[k], 0) / arr.length;
const ndcgAvg = avg(results, 'ndcg');
const mrrAvg = avg(results, 'mrr');
const p3Avg = avg(results, 'p3');
const r10Avg = avg(results, 'r10');

console.log(`\n=== Suite summary (${results.length} queries) ===`);
console.log(`nDCG@3 avg:   ${ndcgAvg.toFixed(3)}  (threshold ≥ 0.7  ${ndcgAvg >= 0.7 ? 'PASS' : 'FAIL'})`);
console.log(`MRR avg:      ${mrrAvg.toFixed(3)}  (threshold ≥ 0.8  ${mrrAvg >= 0.8 ? 'PASS' : 'FAIL'})`);
console.log(`P@3 avg:      ${p3Avg.toFixed(3)}`);
console.log(`Recall@10:    ${r10Avg.toFixed(3)}`);

process.exit(ndcgAvg >= 0.7 && mrrAvg >= 0.8 ? 0 : 1);
