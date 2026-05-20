#!/usr/bin/env node
/**
 * moonshot-proxy.mjs — failover-aware proxy in front of Moonshot's API.
 *
 * Problem: OpenClaw's built-in failover trigger doesn't match Moonshot's
 * "engine_overloaded_error" type — so 429s from Moonshot cause 4 retries
 * of the SAME model instead of failing over to gpt-5.4-mini / claude-haiku.
 * Logs from 2026-05-20 show ~20 failover storms; that one day's
 * retry-waste accounted for ~50% of the Kimi token spike.
 *
 * This proxy:
 *   1. Sits at 127.0.0.1:18790 — OpenClaw points its Moonshot baseUrl here
 *      instead of api.moonshot.ai.
 *   2. Forwards POST /v1/chat/completions to the real Moonshot.
 *   3. On 200 OK: pipes the response through (streaming-safe).
 *   4. On 429 / engine_overloaded_error / 5xx: rewrites the request with
 *      model=gpt-5.4-mini and forwards to OpenAI directly. Returns
 *      OpenAI's response to OpenClaw (which sees a normal success and
 *      doesn't know failover happened — the user just keeps chatting).
 *   5. Circuit breaker: 3 consecutive Moonshot failures within 60s opens
 *      the breaker — next 5 minutes of requests skip Moonshot entirely
 *      and go straight to OpenAI. Stops the retry-storm waste at source.
 *
 * Why hide failover INSIDE the proxy instead of letting OpenClaw do it:
 *   We tried OpenClaw's failover. It doesn't trigger on Moonshot's specific
 *   error shape. Rather than patch OpenClaw upstream, we own the seam here.
 *   This proxy is the only place that needs to know about Moonshot's quirks.
 *
 * Env vars:
 *   MOONSHOT_API_KEY    — from bot-cred vault, set by systemd unit
 *   OPENAI_API_KEY      — same; used for fallback
 *   PROXY_PORT          — listen port (default 18790)
 *   FALLBACK_MODEL      — OpenAI model name to swap to (default gpt-5.4-mini)
 *
 * Logs to stderr — captured by journalctl when running under systemd.
 */

import http from 'node:http';
import https from 'node:https';

const MOONSHOT_HOST = 'api.moonshot.ai';
const OPENAI_HOST = 'api.openai.com';
const MOONSHOT_PATH = '/v1/chat/completions';
const OPENAI_PATH = '/v1/chat/completions';
const PROXY_PORT = parseInt(process.env.PROXY_PORT ?? '18790', 10);
const FALLBACK_MODEL = process.env.FALLBACK_MODEL ?? 'gpt-5.4-mini';
const MOONSHOT_API_KEY = process.env.MOONSHOT_API_KEY ?? '';
const OPENAI_API_KEY = process.env.OPENAI_API_KEY ?? '';

// Circuit breaker state — process-local
const CIRCUIT_FAILURE_THRESHOLD = 3;
const CIRCUIT_FAILURE_WINDOW_MS = 60 * 1000;
const CIRCUIT_OPEN_DURATION_MS = 5 * 60 * 1000;
let recentFailures = []; // array of timestamps
let circuitOpenUntil = 0;

function log(...args) {
  // stderr → journalctl. Prepend ISO timestamp for parsing.
  process.stderr.write(`[moonshot-proxy] ${new Date().toISOString()} ${args.join(' ')}\n`);
}

function recordFailure() {
  const now = Date.now();
  recentFailures.push(now);
  // Drop failures outside the window
  recentFailures = recentFailures.filter(t => now - t < CIRCUIT_FAILURE_WINDOW_MS);
  if (recentFailures.length >= CIRCUIT_FAILURE_THRESHOLD) {
    circuitOpenUntil = now + CIRCUIT_OPEN_DURATION_MS;
    recentFailures = []; // reset; the breaker is open
    log(`circuit breaker OPEN — bypassing Moonshot for ${CIRCUIT_OPEN_DURATION_MS / 60000} min`);
  }
}

function isCircuitOpen() {
  return Date.now() < circuitOpenUntil;
}

function isOverloadError(statusCode, bodyText) {
  if (statusCode === 429) return true;
  if (statusCode >= 500 && statusCode < 600) return true;
  try {
    const j = JSON.parse(bodyText);
    const t = j?.error?.type ?? '';
    const m = j?.error?.message ?? '';
    return (
      t === 'engine_overloaded_error' ||
      m.toLowerCase().includes('overload') ||
      m.toLowerCase().includes('rate limit')
    );
  } catch {
    return false;
  }
}

function rewriteForOpenAI(payload) {
  // OpenClaw sends Moonshot-style request bodies (essentially OpenAI-shape
  // already, since Moonshot's API IS OpenAI-compatible). Swap model name,
  // drop fields OpenAI doesn't accept.
  const out = { ...payload, model: FALLBACK_MODEL };
  // gpt-5.4-mini requires max_completion_tokens, not max_tokens
  if (out.max_tokens !== undefined) {
    out.max_completion_tokens = out.max_tokens;
    delete out.max_tokens;
  }
  // Strip Moonshot-specific params that OpenAI rejects
  delete out.thinking;
  delete out.partial;
  return out;
}

function callUpstream({ host, path, apiKey, body, onResponse, onError }) {
  const req = https.request(
    {
      host,
      port: 443,
      path,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
        'Content-Length': Buffer.byteLength(body),
      },
      timeout: 180_000, // 180s — matches OpenClaw turn budget
    },
    onResponse
  );
  req.on('error', onError);
  req.on('timeout', () => {
    req.destroy(new Error(`${host} request timeout after 180s`));
  });
  req.write(body);
  req.end();
}

function streamResponse(upstreamRes, clientRes) {
  // Forward headers verbatim — preserves streaming SSE / chunked encoding
  const headers = { ...upstreamRes.headers };
  // Remove hop-by-hop headers that Node sets automatically
  delete headers.connection;
  delete headers['keep-alive'];
  clientRes.writeHead(upstreamRes.statusCode, headers);
  upstreamRes.pipe(clientRes);
}

function handleRequest(clientReq, clientRes) {
  if (clientReq.method !== 'POST' || !clientReq.url?.startsWith('/v1/chat/completions')) {
    clientRes.writeHead(404, { 'Content-Type': 'text/plain' });
    clientRes.end('Not Found — proxy only forwards POST /v1/chat/completions\n');
    return;
  }

  let body = '';
  clientReq.on('data', chunk => {
    body += chunk;
  });
  clientReq.on('end', () => {
    let payload;
    try {
      payload = JSON.parse(body);
    } catch (e) {
      clientRes.writeHead(400, { 'Content-Type': 'application/json' });
      clientRes.end(
        JSON.stringify({
          error: { message: 'proxy: invalid JSON body', type: 'invalid_request_error' },
        })
      );
      return;
    }

    if (isCircuitOpen()) {
      log(`circuit open — routing to OpenAI directly`);
      return forwardToOpenAI(payload, clientRes, 'circuit-open');
    }

    return tryMoonshot(payload, clientRes);
  });
  clientReq.on('error', err => {
    log(`client request error: ${err.message}`);
  });
}

function tryMoonshot(payload, clientRes) {
  if (!MOONSHOT_API_KEY) {
    log('MOONSHOT_API_KEY missing — failing over to OpenAI');
    return forwardToOpenAI(payload, clientRes, 'no-moonshot-key');
  }
  const reqBody = JSON.stringify(payload);
  let upstreamHandled = false;
  callUpstream({
    host: MOONSHOT_HOST,
    path: MOONSHOT_PATH,
    apiKey: MOONSHOT_API_KEY,
    body: reqBody,
    onResponse: upstreamRes => {
      const status = upstreamRes.statusCode ?? 0;
      if (status >= 200 && status < 300) {
        // Happy path. Reset the failure window. Stream through.
        recentFailures = [];
        upstreamHandled = true;
        return streamResponse(upstreamRes, clientRes);
      }
      // Error — buffer body to decide failover
      let errBody = '';
      upstreamRes.on('data', chunk => {
        errBody += chunk;
      });
      upstreamRes.on('end', () => {
        upstreamHandled = true;
        if (isOverloadError(status, errBody)) {
          log(`Moonshot ${status} overload → failover to OpenAI`);
          recordFailure();
          return forwardToOpenAI(payload, clientRes, `moonshot-${status}`);
        }
        // Other error — pass through as-is so caller sees real error
        log(`Moonshot ${status} (non-overload) — passing through`);
        clientRes.writeHead(status, { 'Content-Type': 'application/json' });
        clientRes.end(errBody);
      });
    },
    onError: err => {
      if (upstreamHandled) return;
      upstreamHandled = true;
      log(`Moonshot network error: ${err.message} → failover to OpenAI`);
      recordFailure();
      forwardToOpenAI(payload, clientRes, `network-${err.message.slice(0, 30)}`);
    },
  });
}

function forwardToOpenAI(originalPayload, clientRes, reason) {
  if (!OPENAI_API_KEY) {
    log('OPENAI_API_KEY missing — cannot failover');
    clientRes.writeHead(503, { 'Content-Type': 'application/json' });
    clientRes.end(
      JSON.stringify({
        error: {
          message: `proxy: Moonshot failed (${reason}) and OPENAI_API_KEY is not set`,
          type: 'service_unavailable',
        },
      })
    );
    return;
  }
  const rewritten = rewriteForOpenAI(originalPayload);
  const reqBody = JSON.stringify(rewritten);
  let upstreamHandled = false;
  callUpstream({
    host: OPENAI_HOST,
    path: OPENAI_PATH,
    apiKey: OPENAI_API_KEY,
    body: reqBody,
    onResponse: upstreamRes => {
      upstreamHandled = true;
      const status = upstreamRes.statusCode ?? 0;
      log(`OpenAI fallback returned ${status} (reason=${reason})`);
      return streamResponse(upstreamRes, clientRes);
    },
    onError: err => {
      if (upstreamHandled) return;
      upstreamHandled = true;
      log(`OpenAI fallback network error: ${err.message}`);
      clientRes.writeHead(502, { 'Content-Type': 'application/json' });
      clientRes.end(
        JSON.stringify({
          error: {
            message: `proxy: both Moonshot (${reason}) and OpenAI failed: ${err.message}`,
            type: 'bad_gateway',
          },
        })
      );
    },
  });
}

const server = http.createServer(handleRequest);
server.on('error', err => {
  log(`server error: ${err.message}`);
  process.exit(1);
});
server.listen(PROXY_PORT, '127.0.0.1', () => {
  log(`listening on http://127.0.0.1:${PROXY_PORT}${MOONSHOT_PATH}`);
  log(`fallback model: ${FALLBACK_MODEL} (via OpenAI)`);
  if (!MOONSHOT_API_KEY) log('WARN: MOONSHOT_API_KEY not in env — will always fall back to OpenAI');
  if (!OPENAI_API_KEY) log('WARN: OPENAI_API_KEY not in env — failover DISABLED');
});

// Graceful shutdown
['SIGTERM', 'SIGINT'].forEach(sig => {
  process.on(sig, () => {
    log(`received ${sig} — shutting down`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 5000); // hard-stop after 5s
  });
});
