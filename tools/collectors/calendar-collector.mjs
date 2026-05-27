#!/usr/bin/env node
// Phase 4.5 Calendar bootstrap collector.
// Pulls last N days of events from each configured Google Calendar account via
// ClawVisor, with attendee + location + description data.
//
// Outputs:
//   <out>/<account_slug>-raw.json     full event list
//   <out>/<account_slug>-digest.md    agent-readable digest grouped by week
//   <out>/<account_slug>-attendees.json  attendee frequency map (name + email -> count)
//
// Reads CLAWVISOR_URL, CLAWVISOR_AGENT_TOKEN, CLAWVISOR_STANDING_TASK_ID from env.
// Usage: node calendar-collector.mjs [days]

import { writeFileSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
// eslint-disable-next-line import/no-unresolved
const { calendarList } = await import(`${__dirname}/composio_client.mjs`);

// Map email-address style account → Composio calendar connection ID.
// Note: composio "personal" alias maps to the WORK email (mislabeled
// during OAuth); the personal-email calendar has no alias so we use
// its stable account ID.
const COMPOSIO_CAL_ACCOUNT = {
  '':    'personal',
  '${USER_EMAIL}':  'googlecalendar_casse-wikeno',
};

const ACCOUNTS = ['${USER_EMAIL}', ''];
const DAYS = parseInt(process.argv.find(a => /^\d+$/.test(a)) || '60', 10);
const OUT_DIR = process.env.OUT_DIR || './data/calendar';
const MAX_PAGES = parseInt(process.env.MAX_PAGES || '20', 10);

async function gatewayCall({ account, action, params, reason, sessionId }) {
  // Composio-backed replacement for the prior ClawVisor gateway POST.
  // sessionId + reason are dropped (composio doesn't surface them).
  const composioAccount = COMPOSIO_CAL_ACCOUNT[account] || account;
  if (action === 'list_events') {
    return await calendarList({
      account: composioAccount,
      from: params.from,
      to: params.to,
      maxResults: params.max_results || 250,
      pageToken: params.page_token || null,
    });
  }
  throw new Error(`unsupported calendar action: ${action}`);
}

function isoDaysAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

function isoDaysFromNow(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString();
}

async function collectAccount(account) {
  const sessionId = randomUUID();
  const from = isoDaysAgo(DAYS);
  const to = isoDaysFromNow(7);
  const all = [];
  let pageToken;

  for (let page = 1; page <= MAX_PAGES; page++) {
    const params = {
      from,
      to,
      max_results: 250,
      single_events: true,
      order_by: 'startTime',
    };
    if (pageToken) params.page_token = pageToken;
    const result = await gatewayCall({
      account,
      action: 'list_events',
      params,
      reason: `Phase 4.5 bootstrap: paginating last ${DAYS}d (and next 7d) calendar events from ${account} (page ${page}) to extract people, companies, and meetings into brain pages`,
      sessionId,
    });
    const events = Array.isArray(result?.data) ? result.data : (result?.data?.events || result?.data?.items || []);
    all.push(...events);
    pageToken = result?.meta?.next_page_token;
    process.stdout.write(`  ${account} page ${page}: +${events.length} (total ${all.length})\n`);
    if (!pageToken) break;
  }
  return all;
}

function attendeeMap(events) {
  const m = new Map();
  for (const ev of events) {
    for (const att of (ev.attendees || [])) {
      const email = (att.email || '').toLowerCase();
      if (!email) continue;
      const entry = m.get(email) || { email, names: new Set(), count: 0, organizer: 0 };
      if (att.displayName) entry.names.add(att.displayName);
      entry.count += 1;
      if (att.organizer) entry.organizer += 1;
      m.set(email, entry);
    }
  }
  return [...m.values()]
    .map(e => ({ email: e.email, names: [...e.names], count: e.count, organizer: e.organizer }))
    .sort((a, b) => b.count - a.count);
}

function weekKey(isoLike) {
  const d = new Date(isoLike);
  if (isNaN(d.getTime())) return 'unknown';
  const weekStart = new Date(d);
  weekStart.setDate(d.getDate() - d.getDay());
  return weekStart.toISOString().slice(0, 10);
}

function digest(account, events, attendees) {
  const lines = [];
  lines.push(`# Calendar digest — ${account}`);
  lines.push('');
  lines.push(`Generated: ${new Date().toISOString()}`);
  lines.push(`Window: last ${DAYS} days through next 7 days`);
  lines.push(`Events: ${events.length} · Distinct attendees: ${attendees.length}`);
  lines.push('');
  lines.push('## Top attendees');
  lines.push('');
  for (const a of attendees.slice(0, 25)) {
    const names = a.names.length ? a.names.join(' / ') : '(no name)';
    lines.push(`- **${a.count}** — ${names} · ${a.email}${a.organizer ? ` · organized ${a.organizer}` : ''}`);
  }
  lines.push('');

  const byWeek = new Map();
  for (const ev of events) {
    const start = typeof ev.start === 'string' ? ev.start : (ev.start?.dateTime || ev.start?.date || '');
    const wk = weekKey(start);
    if (!byWeek.has(wk)) byWeek.set(wk, []);
    byWeek.get(wk).push(ev);
  }
  const weeks = [...byWeek.keys()].sort().reverse();
  lines.push('## Events by week');
  lines.push('');
  for (const wk of weeks) {
    const weekEvents = byWeek.get(wk);
    lines.push(`### Week of ${wk} (${weekEvents.length})`);
    lines.push('');
    for (const ev of weekEvents) {
      const start = typeof ev.start === 'string' ? ev.start : (ev.start?.dateTime || ev.start?.date || '(unknown)');
      const summary = ev.summary || '(no title)';
      const atts = (ev.attendees || []).map(a => a.displayName || a.email).filter(Boolean);
      lines.push(`- **${start.slice(0, 16)}** — ${summary}`);
      if (atts.length) lines.push(`  - Attendees: ${atts.join(', ')}`);
      if (ev.location) lines.push(`  - Location: ${ev.location}`);
      if (ev.description) {
        const d = ev.description.replace(/\s+/g, ' ').trim().slice(0, 200);
        if (d) lines.push(`  - ${d}`);
      }
    }
    lines.push('');
  }
  return lines.join('\n');
}

async function main() {
  // Composio_client checks COMPOSIO_API_KEY itself; nothing to validate here.
  mkdirSync(OUT_DIR, { recursive: true });
  for (const account of ACCOUNTS) {
    console.log(`[calendar-collector] ${account}: collecting last ${DAYS}d through next 7d`);
    const events = await collectAccount(account);
    const attendees = attendeeMap(events);
    const slug = account.replace('@', '_at_').replace(/\./g, '_');
    writeFileSync(join(OUT_DIR, `${slug}-raw.json`), JSON.stringify({ account, days: DAYS, generated_at: new Date().toISOString(), events }, null, 2));
    writeFileSync(join(OUT_DIR, `${slug}-attendees.json`), JSON.stringify(attendees, null, 2));
    writeFileSync(join(OUT_DIR, `${slug}-digest.md`), digest(account, events, attendees));
    console.log(`[calendar-collector] ${account}: ${events.length} events · ${attendees.length} distinct attendees`);
  }
}

main().catch(e => { console.error(e); process.exit(1); });
