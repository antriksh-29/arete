// composio_client.mjs — Node.js equivalent of infra/composio_client.py.
// Used by tools/collectors/{email,calendar}-collector.mjs after the
// 2026-05-16 ClawVisor → Composio migration.
//
// Reads COMPOSIO_API_KEY from the bot-cred vault. Talks JSON-RPC over
// HTTP to https://connect.composio.dev/mcp, parses the SSE-framed
// response, unwraps the COMPOSIO_MULTI_EXECUTE_TOOL envelope, and
// returns the tool's `data` payload.

import { execSync } from 'node:child_process';

const MCP_URL = 'https://connect.composio.dev/mcp';
let _API_KEY = null;

function apiKey() {
  if (_API_KEY) return _API_KEY;
  _API_KEY = execSync('/usr/local/bin/bot-cred get COMPOSIO_API_KEY', { encoding: 'utf-8' }).trim();
  if (!_API_KEY) throw new Error('COMPOSIO_API_KEY missing from vault');
  return _API_KEY;
}

function parseSse(raw) {
  for (const line of raw.split('\n')) {
    if (line.startsWith('data: ')) {
      try { return JSON.parse(line.slice(6)); }
      catch (_) { continue; }
    }
  }
  try { return JSON.parse(raw); }
  catch (_) { return null; }
}

/**
 * Execute a single Composio tool via COMPOSIO_MULTI_EXECUTE_TOOL.
 * Returns the unwrapped data payload from the tool's response.
 */
export async function execute(toolSlug, args, { account = null, thought = 'collector', timeoutMs = 60_000 } = {}) {
  const toolItem = { tool_slug: toolSlug, arguments: args };
  if (account) toolItem.account = account;
  const body = JSON.stringify({
    jsonrpc: '2.0', id: 1, method: 'tools/call',
    params: {
      name: 'COMPOSIO_MULTI_EXECUTE_TOOL',
      arguments: {
        tools: [toolItem],
        thought,
        sync_response_to_workbench: false,
        current_step: toolSlug,
      },
    },
  });
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);
  let raw;
  try {
    const r = await fetch(MCP_URL, {
      method: 'POST',
      headers: {
        'x-consumer-api-key': apiKey(),
        'Content-Type': 'application/json',
        'Accept': 'application/json,text/event-stream',
      },
      body,
      signal: controller.signal,
    });
    raw = await r.text();
  } finally {
    clearTimeout(t);
  }
  const payload = parseSse(raw);
  if (!payload) throw new Error(`composio ${toolSlug}: unparseable response: ${raw.slice(0, 200)}`);
  if (payload.error) throw new Error(`composio ${toolSlug}: ${JSON.stringify(payload.error)}`);
  const content = payload?.result?.content || [];
  if (!content.length) throw new Error(`composio ${toolSlug}: empty content envelope`);
  let inner;
  try { inner = JSON.parse(content[0].text || '{}'); }
  catch (e) { throw new Error(`composio ${toolSlug}: content not JSON`); }
  const data = inner.data || inner;
  const results = data.results;
  if (Array.isArray(results) && results.length) {
    const first = results[0];
    const resp = first.response || {};
    if (resp.successful === false) {
      throw new Error(`composio ${toolSlug}: ${resp.error || resp.message || JSON.stringify(resp)}`);
    }
    return resp.data || {};
  }
  return data;
}

/** Map a composio GMAIL_FETCH_EMAILS message → clawvisor-shaped fields
 * (id, from, to, subject, snippet, labels, timestamp, is_unread). This
 * keeps the downstream collector logic unchanged after the migration. */
function normalizeGmailMessage(m) {
  const labels = m.labelIds || [];
  return {
    ...m,
    id: m.messageId || m.id,
    from: m.sender || m.from || '',
    to: m.to || '',
    subject: m.subject || '',
    snippet: (m.preview?.body) || m.snippet || '',
    timestamp: m.messageTimestamp || m.timestamp || '',
    labels,
    is_unread: labels.includes('UNREAD'),
  };
}

/** Convenience: gmail search returning a ClawVisor-shaped envelope so
 * existing collectors don't need to know about composio field names. */
export async function gmailSearch({ account, query, maxResults = 100, pageToken = null, verbose = true } = {}) {
  const args = { query, user_id: 'me', verbose, max_results: maxResults };
  if (pageToken) args.page_token = pageToken;
  const data = await execute('GMAIL_FETCH_EMAILS', args, { account });
  const messages = (data.messages || []).map(normalizeGmailMessage);
  const nextToken = data.nextPageToken || data.next_page_token || null;
  return { data: { messages }, meta: nextToken ? { next_page_token: nextToken } : {} };
}

/** Convenience: gmail get_message in clawvisor-compatible envelope.
 * Composio returns the full message; we surface `to`/`cc` at the top
 * level to match what the collector consumes. */
export async function gmailGet({ account, id } = {}) {
  const data = await execute('GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID',
    { message_id: id, user_id: 'me', format: 'full' }, { account });
  const normalized = normalizeGmailMessage(data);
  return { data: normalized };
}

/** Convenience: calendar list_events with paging, in clawvisor-compatible envelope. */
export async function calendarList({ account, from, to, maxResults = 250, pageToken = null } = {}) {
  const args = {
    calendar_id: 'primary',
    single_events: true,
    order_by: 'startTime',
    max_results: maxResults,
    timeZone: 'Asia/Kolkata',
  };
  if (from) args.timeMin = from;
  if (to) args.timeMax = to;
  if (pageToken) args.pageToken = pageToken;
  const data = await execute('GOOGLECALENDAR_EVENTS_LIST', args, { account });
  const items = data.items || [];
  const nextToken = data.nextPageToken || data.next_page_token || null;
  return { data: items, meta: nextToken ? { next_page_token: nextToken } : {} };
}
