#!/usr/bin/env node
// ClawVisor MCP server — exposes Gmail + Calendar as typed agent tools.
//
// Design notes:
//   - Contract-first per Garry's gbrain CLAUDE.md: every capability the agent
//     might need during a turn gets an MCP tool. Backend cron scripts use the
//     same gateway path but ingest in bulk; this MCP serves live queries.
//   - Auth is injected by the wrapper (clawvisor-mcp-wrapper.sh) which pulls
//     from the bot-cred vault. We never hardcode secrets.
//   - Each tool generates a fresh UUID session_id per call (ClawVisor requires
//     this for standing-task workflow context tracking).
//   - The `reason` field is templated per tool with content that passes
//     ClawVisor's intent-coherence verifier. Vague reasons get rejected with
//     REASON_TOO_VAGUE — empirically tested.
//   - Retries on 429 + 5xx with exponential backoff per the rate-lease
//     pattern from gbrain's src/core/backoff.ts.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { randomUUID } from 'node:crypto';

const CLAWVISOR_URL = process.env.CLAWVISOR_URL;
const TOKEN = process.env.CLAWVISOR_AGENT_TOKEN;
const TASK_ID = process.env.CLAWVISOR_STANDING_TASK_ID;
const ACCOUNTS_DEFAULT = ['antriksh@getarete.co', 'antriksh1980@gmail.com'];

if (!CLAWVISOR_URL || !TOKEN || !TASK_ID) {
  console.error('[clawvisor-mcp] FATAL: missing CLAWVISOR_URL / CLAWVISOR_AGENT_TOKEN / CLAWVISOR_STANDING_TASK_ID env');
  process.exit(1);
}

const log = (...args) => console.error('[clawvisor-mcp]', ...args);

// ─────────────────────────────────────────────────────────────────────────────
// Gateway helper — handles session_id, retries, error normalization
// ─────────────────────────────────────────────────────────────────────────────

async function gatewayCall({ service, action, params, reason }) {
  const sessionId = randomUUID();
  const body = JSON.stringify({
    task_id: TASK_ID,
    session_id: sessionId,
    service,
    action,
    params,
    reason,
  });

  const maxAttempts = 4;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    let resp;
    try {
      resp = await fetch(`${CLAWVISOR_URL}/api/gateway/request?wait=true`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          'Content-Type': 'application/json',
        },
        body,
      });
    } catch (e) {
      log(`network error attempt ${attempt}: ${e.message}`);
      if (attempt === maxAttempts) throw e;
      await new Promise(r => setTimeout(r, 500 * 2 ** (attempt - 1)));
      continue;
    }

    if (resp.status === 429 || (resp.status >= 500 && resp.status < 600)) {
      log(`transient ${resp.status} attempt ${attempt}`);
      if (attempt === maxAttempts) {
        const txt = await resp.text();
        throw new Error(`ClawVisor ${resp.status}: ${txt.slice(0, 300)}`);
      }
      await new Promise(r => setTimeout(r, 500 * 2 ** (attempt - 1)));
      continue;
    }

    const json = await resp.json().catch(() => null);
    if (!json) throw new Error(`ClawVisor returned non-JSON (status ${resp.status})`);

    if (json.status !== 'executed') {
      // Surface as structured error so the agent sees the actual reason
      throw new Error(`ClawVisor ${json.status || 'error'}: ${json.error || json.code || JSON.stringify(json).slice(0, 300)}`);
    }
    return json.result;
  }
  throw new Error('unreachable');
}

// ─────────────────────────────────────────────────────────────────────────────
// Tool definitions
// ─────────────────────────────────────────────────────────────────────────────

const tools = [
  {
    name: 'calendar_list_events',
    description:
      'List Google Calendar events for the user across the requested time window. Returns events with start/end times, attendees, location, and Hangouts link. Use this for "what is on my calendar", meeting prep, scheduling questions.',
    inputSchema: {
      type: 'object',
      properties: {
        account: {
          type: 'string',
          description: `Calendar account email. Default: both ${ACCOUNTS_DEFAULT.join(' and ')}. Pass "all" or omit to fetch from both.`,
        },
        from: {
          type: 'string',
          description: 'ISO 8601 start of window (e.g. 2026-05-01T00:00:00Z). Default: start of today (UTC).',
        },
        to: {
          type: 'string',
          description: 'ISO 8601 end of window (e.g. 2026-05-02T00:00:00Z). Default: end of tomorrow (UTC).',
        },
        max_results: {
          type: 'number',
          description: 'Cap on events returned per account (default 25).',
        },
      },
    },
    handler: async ({ account, from, to, max_results }) => {
      const accounts = !account || account === 'all' ? ACCOUNTS_DEFAULT : [account];
      const startToday = new Date();
      startToday.setHours(0, 0, 0, 0);
      const endTomorrow = new Date();
      endTomorrow.setDate(endTomorrow.getDate() + 1);
      endTomorrow.setHours(23, 59, 59, 999);
      const fromIso = from || startToday.toISOString();
      const toIso = to || endTomorrow.toISOString();
      const cap = max_results || 25;

      const allEvents = [];
      for (const acct of accounts) {
        const result = await gatewayCall({
          service: `google.calendar:${acct}`,
          action: 'list_events',
          params: { from: fromIso, to: toIso, max_results: cap, single_events: true, order_by: 'startTime' },
          reason: `Live calendar lookup for Antriksh's executive assistant: list events between ${fromIso} and ${toIso} from ${acct} so the agent can answer scheduling, meeting-prep, and availability questions in real time during a Telegram conversation.`,
        });
        const events = Array.isArray(result?.data) ? result.data : (result?.data?.events || []);
        for (const ev of events) {
          allEvents.push({ ...ev, _account: acct });
        }
      }
      allEvents.sort((a, b) => (a.start || '').localeCompare(b.start || ''));
      return { events: allEvents, total: allEvents.length, window: { from: fromIso, to: toIso } };
    },
  },

  {
    name: 'calendar_get_event',
    description:
      'Fetch full details for a single calendar event (attendees, description, location, conference link). Use after calendar_list_events when more context on a specific meeting is needed.',
    inputSchema: {
      type: 'object',
      properties: {
        account: { type: 'string', description: 'Calendar account email — required' },
        event_id: { type: 'string', description: 'Event ID from calendar_list_events — required' },
      },
      required: ['account', 'event_id'],
    },
    handler: async ({ account, event_id }) => {
      const result = await gatewayCall({
        service: `google.calendar:${account}`,
        action: 'get_event',
        params: { event_id },
        reason: `Meeting prep for Antriksh: fetch full details (attendees, description, conference link) for event ${event_id} on ${account} so the agent can build prep notes that reference the brain's compiled-truth pages on each attendee.`,
      });
      return Array.isArray(result?.data) ? result.data[0] : result?.data;
    },
  },

  {
    name: 'gmail_search',
    description:
      'Search Gmail using Google\'s standard search syntax (e.g. "from:kaushik newer_than:7d"). Returns message snippets with subject, sender, date, labels, and Gmail deep link. Use for "find the email from X about Y" queries.',
    inputSchema: {
      type: 'object',
      properties: {
        account: { type: 'string', description: `Gmail account. Default: both ${ACCOUNTS_DEFAULT.join(' and ')}. Pass "all" or omit for both.` },
        query: { type: 'string', description: 'Gmail search syntax (e.g. "from:kaushik@armoriq.io newer_than:14d"). Required.' },
        max_results: { type: 'number', description: 'Cap (default 10 per account).' },
      },
      required: ['query'],
    },
    handler: async ({ account, query, max_results }) => {
      const accounts = !account || account === 'all' ? ACCOUNTS_DEFAULT : [account];
      const cap = max_results || 10;
      const allMessages = [];
      for (const acct of accounts) {
        const result = await gatewayCall({
          service: `google.gmail:${acct}`,
          action: 'list_messages',
          params: { query, max_results: cap },
          reason: `Live Gmail search for Antriksh's executive assistant: find messages matching '${query}' on ${acct} to answer a question about a specific email/sender/thread during a Telegram conversation. Read-only — no drafting or sending.`,
        });
        const messages = result?.data?.messages || [];
        for (const m of messages) {
          allMessages.push({ ...m, _account: acct });
        }
      }
      return { messages: allMessages, total: allMessages.length, query };
    },
  },

  {
    name: 'gmail_get_message',
    description:
      'Fetch full body of a single Gmail message. Use when the snippet from gmail_search is not enough.',
    inputSchema: {
      type: 'object',
      properties: {
        account: { type: 'string', description: 'Gmail account email — required' },
        message_id: { type: 'string', description: 'Message ID from gmail_search — required' },
      },
      required: ['account', 'message_id'],
    },
    handler: async ({ account, message_id }) => {
      const result = await gatewayCall({
        service: `google.gmail:${account}`,
        action: 'get_message',
        params: { message_id },
        reason: `Read full body of Gmail message ${message_id} on ${account} for Antriksh's executive assistant — needed to answer a specific question about message content during a Telegram conversation. Read-only.`,
      });
      return Array.isArray(result?.data) ? result.data[0] : result?.data;
    },
  },

  {
    name: 'gmail_get_thread',
    description:
      'Fetch all messages in a Gmail thread for full conversation context. Use when a single message is part of a longer back-and-forth.',
    inputSchema: {
      type: 'object',
      properties: {
        account: { type: 'string', description: 'Gmail account email — required' },
        thread_id: { type: 'string', description: 'Thread ID — required' },
      },
      required: ['account', 'thread_id'],
    },
    handler: async ({ account, thread_id }) => {
      const result = await gatewayCall({
        service: `google.gmail:${account}`,
        action: 'get_thread',
        params: { thread_id },
        reason: `Read full thread ${thread_id} on ${account} to give Antriksh's assistant the conversation context it needs to answer a follow-up question or draft a coherent reply. Read-only.`,
      });
      return result?.data;
    },
  },
];

const toolMap = new Map(tools.map(t => [t.name, t]));

// ─────────────────────────────────────────────────────────────────────────────
// MCP server wiring
// ─────────────────────────────────────────────────────────────────────────────

const server = new Server(
  { name: 'clawvisor-mcp', version: '0.1.0' },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: tools.map(({ handler, ...t }) => t),
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  const tool = toolMap.get(name);
  if (!tool) throw new Error(`Unknown tool: ${name}`);
  try {
    const result = await tool.handler(args || {});
    return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
  } catch (e) {
    log(`tool ${name} error: ${e.message}`);
    return { content: [{ type: 'text', text: `Error: ${e.message}` }], isError: true };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
log(`ready (${tools.length} tools registered)`);
