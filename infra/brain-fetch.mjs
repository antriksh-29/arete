#!/usr/bin/env node
// Read-only helper that fetches files from the canonical brain repo
// () via `gh api`. Used by local audits that
// need to verify brain dirs / pages exist without cloning the repo or
// running gbrain on this machine.
//
// Usage (CLI):
//   node infra/brain-fetch.mjs ls             -> list top-level dirs
//   node infra/brain-fetch.mjs ls people      -> list people/
//   node infra/brain-fetch.mjs cat people/garry-tan.md
//   node infra/brain-fetch.mjs exists lists/to-read.md
//
// Usage (module):
//   import { brainLs, brainExists, brainCat } from './brain-fetch.mjs';

import { execFileSync } from 'node:child_process';

const REPO = '';

function gh(path) {
  // Returns parsed JSON for a successful response, or throws.
  const out = execFileSync('gh', ['api', `repos/${REPO}/contents/${path}`], {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return JSON.parse(out);
}

export function brainLs(path = '') {
  const r = gh(path);
  if (!Array.isArray(r)) throw new Error(`not a directory: ${path}`);
  return r.map((e) => ({ name: e.name, type: e.type, path: e.path }));
}

export function brainExists(path) {
  try {
    gh(path);
    return true;
  } catch {
    return false;
  }
}

export function brainCat(path) {
  const r = gh(path);
  if (Array.isArray(r)) throw new Error(`is a directory: ${path}`);
  if (r.encoding !== 'base64') throw new Error(`unexpected encoding: ${r.encoding}`);
  return Buffer.from(r.content, 'base64').toString('utf-8');
}

// CLI
if (import.meta.url === `file://${process.argv[1]}`) {
  const [, , cmd, arg = ''] = process.argv;
  try {
    if (cmd === 'ls') {
      const entries = brainLs(arg);
      for (const e of entries) console.log(`${e.type === 'dir' ? 'd' : '-'} ${e.path}`);
    } else if (cmd === 'cat') {
      process.stdout.write(brainCat(arg));
    } else if (cmd === 'exists') {
      console.log(brainExists(arg) ? 'yes' : 'no');
      process.exit(brainExists(arg) ? 0 : 1);
    } else {
      console.error('usage: brain-fetch.mjs <ls|cat|exists> [path]');
      process.exit(2);
    }
  } catch (e) {
    console.error(`brain-fetch error: ${e.message}`);
    process.exit(1);
  }
}
