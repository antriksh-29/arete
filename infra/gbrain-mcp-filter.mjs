#!/usr/bin/env node
// Thin MCP filter wrapper around `gbrain serve`.
// Reduces the tool surface advertised to OpenClaw (and therefore the system
// prompt overhead per turn) by intercepting the tools/list response and
// removing tools we never use.
//
// Why a wrapper not a config flag: gbrain's MCP server registers all 41
// tools unconditionally and OpenClaw has no per-MCP-server tool allowlist
// (only gateway.tools.deny which blocks execution but not visibility).
// The wrapper sits between OpenClaw and gbrain serve, identical stdio
// JSON-RPC interface, only mutates the tools/list reply.

import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

const KEEP = new Set([
  // Read
  'query', 'search', 'get_page', 'list_pages',
  // Write
  'put_page', 'add_timeline_entry', 'add_link',
  // Graph
  'get_backlinks', 'get_links', 'traverse_graph',
  // Timeline (structured timeline entries — distinct from page-body markdown).
  // Required for forward-looking briefing queries that check "any entity has
  // a dated timeline entry for today?" — added to support the OOO_UNTIL /
  // DEADLINE / SCHEDULED_ACTION daily Gmail workflow per BRAIN_PROPAGATION.md.
  'get_timeline',
  // Diagnostics
  'get_health',
]);

const child = spawn('/home/botuser/.bun/bin/gbrain', ['serve'], {
  stdio: ['pipe', 'pipe', 'inherit'],
  env: process.env,
});

// Forward our stdin → child stdin verbatim (request side, no filtering needed)
process.stdin.pipe(child.stdin);

// Read child stdout line-by-line, filter, forward to our stdout
const rl = createInterface({ input: child.stdout, crlfDelay: Infinity });

rl.on('line', (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    process.stdout.write(line + '\n');
    return;
  }

  // Filter tools/list response
  if (msg.result && Array.isArray(msg.result.tools)) {
    const before = msg.result.tools.length;
    msg.result.tools = msg.result.tools.filter(t => KEEP.has(t.name));
    process.stderr.write(`[gbrain-filter] tools/list filtered: ${before} → ${msg.result.tools.length}\n`);
  }

  process.stdout.write(JSON.stringify(msg) + '\n');
});

child.on('exit', (code) => process.exit(code));
process.on('SIGTERM', () => child.kill('SIGTERM'));
process.on('SIGINT', () => child.kill('SIGINT'));
