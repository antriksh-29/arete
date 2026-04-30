#!/usr/bin/env node
// Phase 4.5 Gmail bootstrap collector.
// Pulls last N days of inbox messages via ClawVisor for each configured account,
// applies deterministic noise filtering, and writes:
//   <out>/<account_slug>-raw.json     full message list (post-filter)
//   <out>/<account_slug>-noise.json   filtered-out senders (audit trail)
//   <out>/<account_slug>-digest.md    agent-readable summary with Gmail deep links
//
// Reads CLAWVISOR_URL, CLAWVISOR_AGENT_TOKEN, CLAWVISOR_STANDING_TASK_ID from env.
// Usage: node email-collector.mjs [days] [--account=<addr>]

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const CLAWVISOR_URL = process.env.CLAWVISOR_URL;
const TOKEN = process.env.CLAWVISOR_AGENT_TOKEN;
const TASK_ID = process.env.CLAWVISOR_STANDING_TASK_ID;

const DEFAULT_ACCOUNTS = ['antriksh1980@gmail.com', 'antriksh@getarete.co'];
const DAYS = parseInt(process.argv.find(a => /^\d+$/.test(a)) || '30', 10);
const accountArg = process.argv.find(a => a.startsWith('--account='));
const ACCOUNTS = accountArg ? [accountArg.split('=')[1]] : DEFAULT_ACCOUNTS;
const OUT_DIR = process.env.OUT_DIR || './data/email';
const MAX_PAGES = parseInt(process.env.MAX_PAGES || '15', 10);

// Inclusion-first filter: keep messages that have at least one signal label
// (Gmail's own personal-category, or user-applied IMPORTANT/STARRED), AND are
// not from a known bulk/automated sender pattern.
const SIGNAL_LABELS = new Set(['CATEGORY_PERSONAL', 'IMPORTANT', 'STARRED']);
const NOISE_SENDER_RE = /(?:^|<|\s)[\w.+-]*(?:noreply|no-reply|notifications?|donotreply|do-not-reply|calendar-notification|mailer-daemon|postmaster|bounces?|automated|alerts?|statements?|billing|transactions?|account-statement|instaalerts|evoting|e-voting|reportsmailer|reply-via|smart-statement|estatement|cashier|broking)[\w.+-]*@/i;

async function gatewayCall({ account, action, params, reason, sessionId }) {
  const r = await fetch(`${CLAWVISOR_URL}/api/gateway/request?wait=true`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id: TASK_ID,
      session_id: sessionId,
      service: `google.gmail:${account}`,
      action,
      params,
      reason,
    }),
  });
  const json = await r.json();
  if (json.status !== 'executed') {
    throw new Error(`Gateway ${action} failed: ${JSON.stringify(json).slice(0, 400)}`);
  }
  return json.result;
}

async function collectAccount(account) {
  const sessionId = randomUUID();
  const all = [];
  let pageToken;
  const query = `newer_than:${DAYS}d in:inbox`;
  for (let page = 1; page <= MAX_PAGES; page++) {
    const params = { max_results: 100, query };
    if (pageToken) params.page_token = pageToken;
    const result = await gatewayCall({
      account,
      action: 'list_messages',
      params,
      reason: `Phase 4.5 bootstrap: paginating last ${DAYS}d inbox from ${account} (page ${page}) to extract people, companies, and meetings into brain pages`,
      sessionId,
    });
    const messages = result?.data?.messages || [];
    all.push(...messages);
    pageToken = result?.meta?.next_page_token;
    process.stdout.write(`  ${account} page ${page}: +${messages.length} (total ${all.length})\n`);
    if (!pageToken) break;
  }
  return all;
}

function classify(messages) {
  const keep = [];
  const noise = [];
  for (const m of messages) {
    const senderNoise = NOISE_SENDER_RE.test(m.from || '');
    const hasSignal = (m.labels || []).some(l => SIGNAL_LABELS.has(l));
    if (hasSignal && !senderNoise) keep.push(m);
    else noise.push(m);
  }
  return { keep, noise };
}

function gmailLink(account, id) {
  return `https://mail.google.com/mail/u/?authuser=${encodeURIComponent(account)}#inbox/${id}`;
}

function topSenders(messages, n = 25) {
  const counts = new Map();
  for (const m of messages) {
    const from = (m.from || '').replace(/\s*<[^>]+>\s*/g, '').replace(/^["']|["']$/g, '').trim();
    counts.set(from, (counts.get(from) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
}

function digest(account, kept, noise) {
  const lines = [];
  lines.push(`# Email digest — ${account}`);
  lines.push('');
  lines.push(`Generated: ${new Date().toISOString()}`);
  lines.push(`Window: last ${DAYS} days, in:inbox`);
  lines.push(`Kept: ${kept.length} · Filtered as noise: ${noise.length}`);
  lines.push('');
  lines.push('## Top senders (kept set)');
  lines.push('');
  for (const [sender, count] of topSenders(kept)) {
    lines.push(`- **${count}** — ${sender}`);
  }
  lines.push('');
  lines.push(`## Messages to triage (${kept.length})`);
  lines.push('');
  for (const m of kept) {
    lines.push(`### ${m.subject || '(no subject)'}`);
    lines.push(`- From: ${m.from}`);
    lines.push(`- Date: ${m.timestamp}`);
    lines.push(`- Status: ${m.is_unread ? 'UNREAD' : 'read'} · Labels: ${(m.labels || []).join(', ') || '(none)'}`);
    lines.push(`- [Open in Gmail](${gmailLink(account, m.id)})`);
    if (m.snippet) lines.push(`- Snippet: ${m.snippet.replace(/\s+/g, ' ').trim().slice(0, 280)}`);
    lines.push('');
  }
  return lines.join('\n');
}

async function main() {
  if (!CLAWVISOR_URL || !TOKEN || !TASK_ID) {
    console.error('Missing env. Need CLAWVISOR_URL, CLAWVISOR_AGENT_TOKEN, CLAWVISOR_STANDING_TASK_ID.');
    process.exit(1);
  }
  mkdirSync(OUT_DIR, { recursive: true });
  for (const account of ACCOUNTS) {
    console.log(`[email-collector] ${account}: collecting last ${DAYS}d (max ${MAX_PAGES} pages)`);
    const messages = await collectAccount(account);
    const { keep, noise } = classify(messages);
    const slug = account.replace('@', '_at_').replace(/\./g, '_');
    writeFileSync(join(OUT_DIR, `${slug}-raw.json`), JSON.stringify({ account, days: DAYS, generated_at: new Date().toISOString(), kept: keep, noise }, null, 2));
    writeFileSync(join(OUT_DIR, `${slug}-digest.md`), digest(account, keep, noise));
    console.log(`[email-collector] ${account}: ${messages.length} fetched · kept ${keep.length} · noise ${noise.length}`);
  }
}

main().catch(e => { console.error(e); process.exit(1); });
