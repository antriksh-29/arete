#!/usr/bin/env node
// Phase 4.5 bootstrap enrichment.
// Reads raw email + calendar JSON from ~/.brain/_raw/, extracts unique entities
// (people, companies, meetings), calls Claude Sonnet 4.6 to generate compiled
// brain pages following identity/PAGE_SCHEMA.md, writes them to ~/.brain/.
//
// Reads ANTHROPIC_API_KEY from env. Usage:
//   node bootstrap-enrich.mjs [--dry-run] [--only=people|companies|meetings]
//   ENRICH_LIMIT=5 node bootstrap-enrich.mjs   # limit per type for testing

import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const RAW_EMAIL_DIR = process.env.RAW_EMAIL_DIR || join(homedir(), '.brain/_raw/email');
const RAW_CAL_DIR = process.env.RAW_CAL_DIR || join(homedir(), '.brain/_raw/calendar');
const BRAIN_DIR = process.env.BRAIN_DIR || join(homedir(), '.brain');
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
const MODEL = process.env.ENRICH_MODEL || 'claude-sonnet-4-6';
const DRY_RUN = process.argv.includes('--dry-run');
const ONLY = (process.argv.find(a => a.startsWith('--only=')) || '').split('=')[1] || '';
const LIMIT = parseInt(process.env.ENRICH_LIMIT || '0', 10);

const TODAY = new Date().toISOString().slice(0, 10);
const PERSONAL_DOMAINS = new Set(['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'me.com', 'protonmail.com', 'live.com', 'aol.com']);
// Domains that are not "companies the user works with" — banks, SaaS notifications,
// transactional senders, etc. Skip company-page generation for these.
const NOISE_DOMAINS = new Set([
  'slack.com', 'substack.com', 'aws.com', 'mailer.zerodha.com',
  'hdfcbank.bank.in', 'bankofbaroda.bank.in', 'deutsche.bank.in', 'federalbank.co.in',
  'kfintech.com', 'calendar.luma-mail.com', 'reportsmailer.zerodha.net',
  'ppsskoda.com', 'mailer.zerodha.com', 'thepeakai.com',
]);
const SELF_EMAILS = new Set(['antriksh1980@gmail.com', 'antriksh@getarete.co']);

function slugify(s) {
  return (s || '')
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .slice(0, 60) || 'unnamed';
}

function parseFromHeader(from) {
  if (!from) return { name: '', email: '' };
  const match = from.match(/^(.*?)\s*<([^>]+)>\s*$/) || from.match(/^([^<]+)$/);
  if (match && match[2]) return { name: match[1].replace(/^["']|["']$/g, '').trim(), email: match[2].trim().toLowerCase() };
  if (match && match[1]) return { name: '', email: match[1].trim().toLowerCase() };
  return { name: '', email: '' };
}

// Parse a `to:` header which may have multiple comma-separated recipients.
function parseRecipients(to) {
  if (!to) return [];
  // Split by comma, but preserve quoted names with commas (e.g. "Last, First" <email>)
  const parts = [];
  let depth = 0, buf = '';
  for (const c of to) {
    if (c === '<') depth++;
    else if (c === '>') depth--;
    if (c === ',' && depth === 0) { parts.push(buf); buf = ''; }
    else buf += c;
  }
  if (buf.trim()) parts.push(buf);
  return parts.map(p => parseFromHeader(p.trim())).filter(r => r.email);
}

function loadRaw(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter(f => f.endsWith('-raw.json'))
    .map(f => JSON.parse(readFileSync(join(dir, f), 'utf-8')));
}

function buildPeopleMap(emailRaw, calendarRaw) {
  const map = new Map(); // email -> { email, names: Set, email_count, meeting_count, emails: [], meetings: [] }
  const upsert = (email, name) => {
    if (!email || SELF_EMAILS.has(email)) return null;
    const key = email.toLowerCase();
    let entry = map.get(key);
    if (!entry) {
      entry = { email: key, names: new Set(), email_count: 0, meeting_count: 0, emails: [], meetings: [], domains: new Set() };
      map.set(key, entry);
    }
    if (name) entry.names.add(name);
    const domain = key.split('@')[1] || '';
    if (domain) entry.domains.add(domain);
    return entry;
  };

  for (const acct of emailRaw) {
    for (const m of (acct.kept || [])) {
      // SENT messages: the user is the FROM; entities are the recipients (TO).
      // INBOX messages: entity is the FROM (counterparty).
      if (m._folder === 'sent') {
        const recipients = parseRecipients(m.to);
        for (const r of recipients) {
          const entry = upsert(r.email, r.name);
          if (entry) {
            entry.email_count += 1;
            entry.emails.push({
              subject: m.subject,
              snippet: m.snippet,
              timestamp: m.timestamp,
              labels: m.labels,
              from_account: acct.account,
              direction: 'outbound',
              message_id: m.id,
            });
          }
        }
      } else {
        const { name, email } = parseFromHeader(m.from);
        const entry = upsert(email, name);
        if (entry) {
          entry.email_count += 1;
          entry.emails.push({
            subject: m.subject,
            snippet: m.snippet,
            timestamp: m.timestamp,
            labels: m.labels,
            to_account: acct.account,
            direction: 'inbound',
            message_id: m.id,
          });
        }
      }
    }
  }
  for (const acct of calendarRaw) {
    for (const ev of (acct.events || [])) {
      for (const att of (ev.attendees || [])) {
        const entry = upsert((att.email || '').toLowerCase(), att.displayName || '');
        if (entry) {
          entry.meeting_count += 1;
          entry.meetings.push({
            id: ev.id,
            summary: ev.summary,
            start: typeof ev.start === 'string' ? ev.start : ev.start?.dateTime,
            description: (ev.description || '').slice(0, 500),
            organizer: att.organizer || false,
            via_account: acct.account,
          });
        }
      }
    }
  }
  return map;
}

function buildCompanyMap(peopleMap) {
  const map = new Map(); // domain -> { domain, name, people: [] }
  for (const person of peopleMap.values()) {
    for (const domain of person.domains) {
      if (PERSONAL_DOMAINS.has(domain) || NOISE_DOMAINS.has(domain)) continue;
      let entry = map.get(domain);
      if (!entry) {
        entry = { domain, name: domain.split('.')[0], people: [] };
        map.set(domain, entry);
      }
      entry.people.push({ email: person.email, names: [...person.names] });
    }
  }
  for (const e of map.values()) {
    if (e.people.length < 1) map.delete(e.domain);
  }
  return map;
}

function buildMeetings(calendarRaw) {
  const list = [];
  for (const acct of calendarRaw) {
    for (const ev of (acct.events || [])) {
      list.push({
        id: ev.id,
        summary: ev.summary || '(untitled)',
        start: typeof ev.start === 'string' ? ev.start : ev.start?.dateTime,
        end: typeof ev.end === 'string' ? ev.end : ev.end?.dateTime,
        location: ev.location || '',
        description: ev.description || '',
        attendees: (ev.attendees || []).map(a => ({ email: a.email, name: a.displayName, organizer: a.organizer })),
        via_account: acct.account,
        hangoutLink: ev.hangoutLink || '',
      });
    }
  }
  return list;
}

// Provider detection: route to OpenAI for gpt-* models, Anthropic for everything else.
// Both APIs return text; we normalize to a single string.
async function callLLM(systemPrompt, userPrompt) {
  if (MODEL.startsWith('gpt-')) return callOpenAI(systemPrompt, userPrompt);
  return callAnthropic(systemPrompt, userPrompt);
}

async function callAnthropic(systemPrompt, userPrompt) {
  const r = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: 1500,
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
    }),
  });
  if (!r.ok) throw new Error(`Anthropic ${r.status}: ${await r.text()}`);
  const json = await r.json();
  return json.content?.[0]?.text || '';
}

async function callOpenAI(systemPrompt, userPrompt) {
  if (!process.env.OPENAI_API_KEY) {
    throw new Error('OPENAI_API_KEY missing — required when ENRICH_MODEL is gpt-*');
  }
  const r = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${process.env.OPENAI_API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userPrompt },
      ],
      max_completion_tokens: 1500,
    }),
  });
  if (!r.ok) throw new Error(`OpenAI ${r.status}: ${await r.text()}`);
  const json = await r.json();
  return json.choices?.[0]?.message?.content || '';
}

const SYSTEM_PROMPT = `You generate compiled-truth brain pages for Antriksh's personal assistant. Follow this schema EXACTLY.

Output a valid markdown file with YAML frontmatter. NO commentary before or after — just the page content.

Frontmatter fields:
- type: one of person, company, meeting
- name: display name
- aliases: array of alternate names/handles (can be empty: [])
- created: ${TODAY}
- updated: ${TODAY}
- tags: array of relevant tags (lowercase, dashed)

DO NOT include a "slug" field — it is path-derived.

Body structure (PAGE_SCHEMA.md):
1. # Display Name (h1)
2. ## Summary — one paragraph compiled truth, every claim cited [Source: ...]
3. ## Key facts — bullets, each cited
4. Type-specific sections (see below)
5. ## Recent activity — timeline entries, dated, cited

Type-specific sections:
- person: Recurring topics, Open threads, Recent activity (with email/meeting timeline)
- company: Recurring topics, Recent activity
- meeting: Attendees, Decisions, Action items, then NO timeline (meetings are leaf pages)

Citation format: [Source: <origin>, <date>]. Origins: "Gmail, msg from <sender>, <date>", "Calendar, event <id>, <date>", "User, brain seed, <date>".

Entity links: use [[person:slug]], [[company:slug]] for cross-references. Slugs are lowercase-dashed.

Do NOT invent facts. If something is unknown, omit it. Only state what the source data supports. If the data is sparse (e.g. one email, no meetings), keep the page short and honest — do not pad.

Compiled truth above the timeline must not contradict the timeline. Every fact must trace to a source in the data given.`;

function personPrompt(person) {
  const names = [...person.names].filter(Boolean);
  const primaryName = names[0] || person.email.split('@')[0];
  const isCompanyDomain = ![...person.domains].some(d => PERSONAL_DOMAINS.has(d));
  const companyDomain = [...person.domains].find(d => !PERSONAL_DOMAINS.has(d));

  const data = {
    primary_name: primaryName,
    aliases: names.slice(1),
    email: person.email,
    company_domain: companyDomain || null,
    is_company_email: isCompanyDomain,
    email_count: person.email_count,
    meeting_count: person.meeting_count,
    sample_emails: person.emails.slice(0, 5).map(e => ({ subject: e.subject, snippet: (e.snippet || '').slice(0, 200), date: e.timestamp })),
    meetings: person.meetings.slice(0, 8).map(m => ({ summary: m.summary, start: m.start, organizer: m.organizer })),
  };

  return `Generate a person brain page for the following entity.

Entity data (only use this; do not invent):
\`\`\`json
${JSON.stringify(data, null, 2)}
\`\`\`

Slug for entity links pointing to this person: ${slugify(primaryName)}
${companyDomain ? `Their company slug: ${slugify(companyDomain.split('.')[0])} (use [[company:${slugify(companyDomain.split('.')[0])}]] when referring to their company)` : ''}

Generate the markdown page now.`;
}

function companyPrompt(company) {
  const data = {
    domain: company.domain,
    inferred_name: company.name,
    people: company.people.map(p => ({ name: p.names[0] || p.email.split('@')[0], email: p.email })),
  };
  return `Generate a company brain page.

Entity data (only use this; do not invent):
\`\`\`json
${JSON.stringify(data, null, 2)}
\`\`\`

Use [[person:<slug>]] entity links to cross-reference the people. Slug each person from their name (lowercase-dashed).

Generate the markdown page now.`;
}

function meetingPrompt(meeting) {
  const data = {
    id: meeting.id,
    summary: meeting.summary,
    start: meeting.start,
    end: meeting.end,
    location: meeting.location,
    description: (meeting.description || '').slice(0, 1000),
    attendees: meeting.attendees,
  };
  return `Generate a meeting brain page (leaf page — no timeline section).

Entity data:
\`\`\`json
${JSON.stringify(data, null, 2)}
\`\`\`

Notes:
- The user is Antriksh (antriksh@getarete.co or antriksh1980@gmail.com); do not list him as an external attendee.
- Attendees use [[person:<slug>]] links where slug derives from the name.
- "Decisions" and "Action items" sections — leave empty if the description doesn't surface any.

Generate the markdown page now.`;
}

function clean(md) {
  return md.replace(/^```(?:markdown|md)?\n?/i, '').replace(/\n?```\s*$/i, '').trim() + '\n';
}

// Find the brain-page filepath that mentions a given email address.
function findPageByEmail(dir, email) {
  if (!existsSync(dir)) return null;
  const target = email.toLowerCase();
  for (const f of readdirSync(dir).filter(f => f.endsWith('.md'))) {
    const fp = join(dir, f);
    const body = readFileSync(fp, 'utf-8').toLowerCase();
    if (body.includes(target)) return fp;
  }
  return null;
}

// Append new timeline entries to an existing entity page.
// Idempotent: dedups by message_id (or date+subject hash if no message_id).
// Returns the number of entries added.
function appendTimelineEntries(pagePath, person) {
  const body = readFileSync(pagePath, 'utf-8');

  // Collect candidate entries from the new email + meeting batch
  const candidates = [];
  for (const e of (person.emails || [])) {
    if (!e.timestamp) continue;
    const date = new Date(e.timestamp).toISOString().slice(0, 10);
    if (Number.isNaN(new Date(e.timestamp).getTime())) continue;
    const direction = e.direction === 'outbound' ? 'sent' : 'received';
    const subject = (e.subject || '(no subject)').replace(/[\[\]]/g, '');
    const key = e.message_id || `${date}::${subject}`;
    candidates.push({
      key,
      date,
      timestamp: e.timestamp,
      bullet: `- **${date}** — ${direction}: ${subject} [Source: Gmail msg ${e.message_id || '(no id)'}, ${date}]`,
    });
  }
  for (const m of (person.meetings || [])) {
    if (!m.start) continue;
    const date = new Date(m.start).toISOString().slice(0, 10);
    if (Number.isNaN(new Date(m.start).getTime())) continue;
    const summary = (m.summary || '(untitled meeting)').replace(/[\[\]]/g, '');
    const key = m.id || `${date}::${summary}`;
    candidates.push({
      key,
      date,
      timestamp: m.start,
      bullet: `- **${date}** — meeting: ${summary} [Source: Calendar event ${m.id || '(no id)'}, ${date}]`,
    });
  }

  if (candidates.length === 0) return 0;

  // Dedup: skip any candidate whose key/message_id/start_date+summary already
  // appears anywhere in the existing page body.
  const fresh = candidates.filter(c => {
    if (c.key && body.includes(c.key)) return false;
    // also skip if a bullet with the same date and exact subject/summary text is already there
    const m = c.bullet.match(/— \w+: (.+?) \[Source/);
    if (m) {
      const subj = m[1];
      if (body.includes(subj) && body.includes(c.date)) return false;
    }
    return true;
  });

  if (fresh.length === 0) return 0;

  // Sort by timestamp ascending, then prepend the bullets to the start of
  // ## Recent activity (newest at top).
  fresh.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  const newBullets = fresh.map(c => c.bullet).join('\n');

  let updated;
  const recentMatch = body.match(/(## Recent activity\s*\n)/);
  if (recentMatch) {
    // Insert new bullets immediately after the section header, before existing entries.
    updated = body.replace(recentMatch[1], `${recentMatch[1]}\n${newBullets}\n`);
  } else {
    // No Recent activity section — append one at the end.
    updated = body.replace(/\s*$/, `\n\n## Recent activity\n\n${newBullets}\n`);
  }

  // Bump frontmatter `updated:` field if present.
  updated = updated.replace(/^updated:\s*\d{4}-\d{2}-\d{2}/m, `updated: ${TODAY}`);

  writeFileSync(pagePath, updated);
  return fresh.length;
}

async function enrichPeople(peopleMap) {
  // Threshold: must either have a meeting (real human you sat with) OR multiple
  // emails plus a real-looking name. This filters one-shot newsletters/transactional
  // senders that slip past the email collector's regex via CATEGORY_PERSONAL.
  const candidates = [...peopleMap.values()]
    .filter(p => p.meeting_count >= 1 || (p.email_count >= 2 && p.names.size > 0))
    .sort((a, b) => (b.email_count + b.meeting_count * 2) - (a.email_count + a.meeting_count * 2));
  const slice = LIMIT ? candidates.slice(0, LIMIT) : candidates;
  console.log(`[enrich] people: ${slice.length} candidates (of ${candidates.length})`);

  // Build set of existing person slugs and email aliases so we don't write
  // duplicates or recreate pages the user has explicitly deleted.
  const existingSlugs = new Set();
  const existingEmails = new Set();
  const peopleDir = join(BRAIN_DIR, 'people');
  if (existsSync(peopleDir)) {
    for (const f of readdirSync(peopleDir).filter(f => f.endsWith('.md'))) {
      existingSlugs.add(f.replace(/\.md$/, ''));
      // Cheap alias detection: scan for email addresses in the file body.
      const body = readFileSync(join(peopleDir, f), 'utf-8');
      for (const m of body.matchAll(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi)) {
        existingEmails.add(m[0].toLowerCase());
      }
    }
  }

  for (const p of slice) {
    // EXISTING ENTITY: append new timeline entries deterministically (no LLM).
    // This is the fix for "brain page goes stale once it exists" — daily collector
    // now keeps existing pages fresh with each new email/meeting.
    if (existingEmails.has(p.email)) {
      const existingPath = findPageByEmail(peopleDir, p.email);
      if (existingPath) {
        const added = appendTimelineEntries(existingPath, p);
        if (added > 0) console.log(`  update ${existingPath.replace(BRAIN_DIR + '/', '')} (+${added} timeline entries)`);
        else console.log(`  skip ${p.email} (no new activity)`);
      } else {
        console.log(`  skip ${p.email} (page exists but couldn't locate file)`);
      }
      continue;
    }
    // Prefer longest name (most specific) for stable slugging across runs.
    const sortedNames = [...p.names].sort((a, b) => b.length - a.length);
    const primaryName = sortedNames[0] || p.email.split('@')[0];
    const slug = slugify(primaryName);
    // Skip cryptic gmail-handle slugs (no real name available — page would be
    // a low-value stub. Agent will create a proper page when user mentions
    // them by their real name).
    if (sortedNames.length === 0 && /^[a-z0-9_.-]+$/.test(slug) && !slug.includes('-')) {
      console.log(`  skip ${slug} (cryptic email-handle slug)`);
      continue;
    }
    if (existingSlugs.has(slug)) { console.log(`  skip ${slug} (slug exists)`); continue; }
    const path = join(BRAIN_DIR, 'people', `${slug}.md`);
    if (existsSync(path)) { console.log(`  skip ${slug} (exists)`); continue; }
    console.log(`  enrich ${slug} <- ${p.email} (${p.email_count}e/${p.meeting_count}m)`);
    if (DRY_RUN) continue;
    try {
      const md = clean(await callLLM(SYSTEM_PROMPT, personPrompt(p)));
      mkdirSync(join(BRAIN_DIR, 'people'), { recursive: true });
      writeFileSync(path, md);
    } catch (e) { console.error(`  ERROR ${slug}: ${e.message}`); }
  }
}

async function enrichCompanies(companyMap) {
  const candidates = [...companyMap.values()].sort((a, b) => b.people.length - a.people.length);
  const slice = LIMIT ? candidates.slice(0, LIMIT) : candidates;
  console.log(`[enrich] companies: ${slice.length} candidates`);
  for (const c of slice) {
    const slug = slugify(c.name);
    const path = join(BRAIN_DIR, 'companies', `${slug}.md`);
    if (existsSync(path)) { console.log(`  skip ${slug} (exists)`); continue; }
    console.log(`  enrich ${slug} <- ${c.domain} (${c.people.length} people)`);
    if (DRY_RUN) continue;
    try {
      const md = clean(await callLLM(SYSTEM_PROMPT, companyPrompt(c)));
      mkdirSync(join(BRAIN_DIR, 'companies'), { recursive: true });
      writeFileSync(path, md);
    } catch (e) { console.error(`  ERROR ${slug}: ${e.message}`); }
  }
}

async function enrichMeetings(meetings) {
  // Skip personal-scheduling and self-only events; only enrich meetings with
  // a real counterparty (>=1 non-self attendee) and a substantive title.
  const SKIP_TITLE_RE = /^(?:busy|out|away|otp|wfh|lunch|gym|tennis|flight to|train to|try booking|book |buy |grocery|laundry|meditation|workout|read|reading)\b/i;
  const filtered = meetings.filter(m => {
    if (SKIP_TITLE_RE.test((m.summary || '').trim())) return false;
    const others = (m.attendees || []).filter(a => !SELF_EMAILS.has((a.email || '').toLowerCase()));
    return others.length >= 1;
  });
  const sorted = filtered.sort((a, b) => (b.start || '').localeCompare(a.start || ''));
  const slice = LIMIT ? sorted.slice(0, LIMIT) : sorted;
  console.log(`[enrich] meetings: ${slice.length} candidates`);
  for (const m of slice) {
    const datePart = (m.start || '').slice(0, 10);
    const slug = `${datePart}-${slugify(m.summary).slice(0, 40)}`;
    const path = join(BRAIN_DIR, 'meetings', `${slug}.md`);
    if (existsSync(path)) { console.log(`  skip ${slug} (exists)`); continue; }
    console.log(`  enrich ${slug}`);
    if (DRY_RUN) continue;
    try {
      const md = clean(await callLLM(SYSTEM_PROMPT, meetingPrompt(m)));
      mkdirSync(join(BRAIN_DIR, 'meetings'), { recursive: true });
      writeFileSync(path, md);
    } catch (e) { console.error(`  ERROR ${slug}: ${e.message}`); }
  }
}

async function main() {
  if (!ANTHROPIC_API_KEY) { console.error('Missing ANTHROPIC_API_KEY'); process.exit(1); }
  const emailRaw = loadRaw(RAW_EMAIL_DIR);
  const calRaw = loadRaw(RAW_CAL_DIR);
  console.log(`[load] ${emailRaw.length} email files, ${calRaw.length} calendar files`);
  const peopleMap = buildPeopleMap(emailRaw, calRaw);
  const companyMap = buildCompanyMap(peopleMap);
  const meetings = buildMeetings(calRaw);
  console.log(`[extract] ${peopleMap.size} people, ${companyMap.size} companies, ${meetings.length} meetings`);
  if (!ONLY || ONLY === 'people') await enrichPeople(peopleMap);
  if (!ONLY || ONLY === 'companies') await enrichCompanies(companyMap);
  if (!ONLY || ONLY === 'meetings') await enrichMeetings(meetings);
  console.log('[done]');
}

main().catch(e => { console.error(e); process.exit(1); });
