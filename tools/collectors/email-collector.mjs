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
import { join, dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
// eslint-disable-next-line import/no-unresolved
const { gmailSearch, gmailGet } = await import(`${__dirname}/composio_client.mjs`);

// Map email-address style account → Composio account alias / connection ID.
// Composio routes by alias ("work", "personal") or stable account ID.
const COMPOSIO_ALIAS = {
  'antriksh@getarete.co': 'work',
  'antriksh1980@gmail.com': 'personal',
};

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
  // Composio-backed replacement for the prior ClawVisor gateway POST.
  // sessionId + reason are ignored (composio doesn't surface them).
  const composioAccount = COMPOSIO_ALIAS[account] || account;
  if (action === 'list_messages') {
    return await gmailSearch({
      account: composioAccount,
      query: params.query,
      maxResults: params.max_results || 100,
      pageToken: params.page_token || null,
      verbose: true,
    });
  }
  if (action === 'get_message') {
    return await gmailGet({ account: composioAccount, id: params.id || params.message_id });
  }
  throw new Error(`unsupported gmail action: ${action}`);
}

async function collectFolder(account, folder, sessionId) {
  const all = [];
  let pageToken;
  const query = `newer_than:${DAYS}d in:${folder}`;
  for (let page = 1; page <= MAX_PAGES; page++) {
    const params = { max_results: 100, query };
    if (pageToken) params.page_token = pageToken;
    const result = await gatewayCall({
      account,
      action: 'list_messages',
      params,
      reason: `Phase 4.5 bootstrap: paginating last ${DAYS}d ${folder} from ${account} (page ${page}) to extract people, companies, and meetings into brain pages with both inbound and outbound activity`,
      sessionId,
    });
    const messages = result?.data?.messages || [];
    // Tag each message with folder so enrichment knows direction
    for (const m of messages) m._folder = folder;
    all.push(...messages);
    pageToken = result?.meta?.next_page_token;
    process.stdout.write(`  ${account} ${folder} page ${page}: +${messages.length} (total ${all.length})\n`);
    if (!pageToken) break;
  }
  return all;
}

async function collectAccount(account) {
  const sessionId = randomUUID();
  // Pull both inbox (inbound) and sent (outbound). Outbound activity is what
  // catches user-initiated follow-ups for the morning briefing.
  const inbox = await collectFolder(account, 'inbox', sessionId);
  const sent = await collectFolder(account, 'sent', sessionId);

  // ClawVisor's list_messages omits the `to:` field for sent folder, so we
  // need a follow-up get_message call per sent message to extract recipients.
  // Bounded to avoid runaway cost (typical day: 0-5 sent emails per account).
  for (const m of sent) {
    if (!m.id) continue;
    if (m.to) continue; // Already populated (defensive)
    try {
      const result = await gatewayCall({
        account,
        action: 'get_message',
        params: { message_id: m.id },
        reason: `Daily digest collection: read full headers (to, cc, subject) for sent message ${m.id} on ${account}. The list_messages action does not return the to: field for sent folder, so a follow-up get_message read is required to know which counterparty this thread relates to. Read-only — same intent as the daily digest scope (bulk ingestion + ongoing daily digest collection).`,
        sessionId,
      });
      const data = Array.isArray(result?.data) ? result.data[0] : result?.data;
      if (data?.to) m.to = data.to;
      if (data?.cc) m.cc = data.cc;
    } catch (e) {
      process.stderr.write(`  ${account} get_message ${m.id} failed: ${e.message}\n`);
    }
  }

  // Dedup by message id (sent + reply could appear in both views in edge cases)
  const seen = new Set();
  const all = [];
  for (const m of [...inbox, ...sent]) {
    if (m.id && seen.has(m.id)) continue;
    if (m.id) seen.add(m.id);
    all.push(m);
  }
  return all;
}

function classify(messages) {
  const keep = [];
  const noise = [];
  for (const m of messages) {
    // SENT folder messages: keep all (user explicitly composed/sent these — high signal by definition).
    if (m._folder === 'sent') {
      keep.push(m);
      continue;
    }
    // INBOX: signal-label gating + noise-sender filter.
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
  // Composio_client checks COMPOSIO_API_KEY itself; nothing to validate here.
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
