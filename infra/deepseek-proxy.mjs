#!/usr/bin/env node
/**
 * deepseek-proxy.mjs — failover-aware proxy in front of DeepSeek's API.
 *
 * Mirrors infra/moonshot-proxy.mjs in design. Sits at 127.0.0.1:18791
 * and OpenClaw points its DeepSeek baseUrl here instead of
 * api.deepseek.com. On overload/5xx/429/network-error from DeepSeek,
 * the request is rewritten with model=kimi-k2.6 and forwarded to the
 * MOONSHOT PROXY (127.0.0.1:18790), which already has its own
 * Kimi→OpenAI failover + circuit breaker. So one DeepSeek failure
 * triggers the entire downstream chain transparently.
 *
 * Why chain through the moonshot proxy instead of api.moonshot.ai
 * directly: layered breakers. If both Moonshot and DeepSeek are
 * unhappy at the same time (peak hours), the moonshot proxy still
 * bounces to OpenAI without us having to teach this proxy about
 * three different upstreams.
 *
 * Why ALSO have OpenClaw native fallbacks configured:
 *   - This proxy hides ONE level of failover (deepseek → kimi).
 *     If the moonshot-proxy can't recover either, the original 5xx
 *     bubbles up and OpenClaw's native fallback chain takes over
 *     (claude-haiku-4-5 → gpt-5.4-mini).
 *   - Defense in depth. DeepSeek has reported 503s at peak hours,
 *     plus a known tool-call schema bug (intermittent — emits
 *     tool_calls as plain text in `content`). Want every layer
 *     able to recover.
 *
 * Thinking mode toggle:
 *   DEEPSEEK_THINKING=disabled (default) — adds `thinking:{type:"disabled"}`
 *     to every request. Mirrors the Kimi instant-mode discipline that
 *     prevents 180s gateway timeouts.
 *   DEEPSEEK_THINKING=enabled — adds `thinking:{type:"enabled"}` + the
 *     `reasoning_effort` field if specified.
 *   DEEPSEEK_THINKING=passthrough — leaves whatever the caller sent.
 *
 * Env vars:
 *   DEEPSEEK_API_KEY    — from bot-cred vault, set by systemd unit
 *   PROXY_PORT          — listen port (default 18791)
 *   FALLBACK_PROXY_PORT — moonshot-proxy port (default 18790)
 *   FALLBACK_MODEL      — model name to swap to on failover (default moonshot-v1-128k-fallback)
 *   DEEPSEEK_THINKING   — disabled | enabled | passthrough (default disabled)
 *   DEEPSEEK_REASONING_EFFORT — low | medium | high (default unset; only used when thinking=enabled)
 */

import http from 'node:http';
import https from 'node:https';

const DEEPSEEK_HOST = 'api.deepseek.com';
const DEEPSEEK_PATH = '/v1/chat/completions';
const FALLBACK_HOST = '127.0.0.1';
const FALLBACK_PROXY_PORT = parseInt(process.env.FALLBACK_PROXY_PORT ?? '18790', 10);
const FALLBACK_PATH = '/v1/chat/completions';
const PROXY_PORT = parseInt(process.env.PROXY_PORT ?? '18791', 10);
const FALLBACK_MODEL = process.env.FALLBACK_MODEL ?? 'kimi-k2.6';
const DEEPSEEK_API_KEY = process.env.DEEPSEEK_API_KEY ?? '';
const THINKING_MODE = (process.env.DEEPSEEK_THINKING ?? 'disabled').toLowerCase();
const REASONING_EFFORT = process.env.DEEPSEEK_REASONING_EFFORT ?? '';

const CIRCUIT_FAILURE_THRESHOLD = 3;
const CIRCUIT_FAILURE_WINDOW_MS = 60 * 1000;
const CIRCUIT_OPEN_DURATION_MS = 5 * 60 * 1000;
let recentFailures = [];
let circuitOpenUntil = 0;

function log(...args) {
  process.stderr.write(`[deepseek-proxy] ${new Date().toISOString()} ${args.join(' ')}\n`);
}

function recordFailure() {
  const now = Date.now();
  recentFailures.push(now);
  recentFailures = recentFailures.filter(t => now - t < CIRCUIT_FAILURE_WINDOW_MS);
  if (recentFailures.length >= CIRCUIT_FAILURE_THRESHOLD) {
    circuitOpenUntil = now + CIRCUIT_OPEN_DURATION_MS;
    recentFailures = [];
    log(`circuit breaker OPEN — bypassing DeepSeek for ${CIRCUIT_OPEN_DURATION_MS / 60000} min`);
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
      t === 'server_error' ||
      t === 'engine_overloaded_error' ||
      m.toLowerCase().includes('overload') ||
      m.toLowerCase().includes('rate limit') ||
      m.toLowerCase().includes('service unavailable')
    );
  } catch {
    return false;
  }
}

function applyThinkingPolicy(payload) {
  // Mutate in place per DEEPSEEK_THINKING env var. DeepSeek accepts the
  // same `{"thinking": {"type": "enabled"|"disabled"}}` shape as Moonshot.
  if (THINKING_MODE === 'passthrough') return payload;
  if (THINKING_MODE === 'enabled') {
    payload.thinking = { type: 'enabled' };
    if (REASONING_EFFORT) payload.reasoning_effort = REASONING_EFFORT;
    return payload;
  }
  // default: disabled — instant mode, matches Kimi discipline
  payload.thinking = { type: 'disabled' };
  delete payload.reasoning_effort;
  return payload;
}

function rewriteForFallback(payload) {
  // Swap model + strip DeepSeek-specific fields. Moonshot proxy expects
  // Kimi-shaped request bodies; both DeepSeek and Moonshot are OpenAI-
  // chat-compatible so the rest of the payload (messages, tools, etc.)
  // passes through unchanged.
  const out = { ...payload, model: FALLBACK_MODEL };
  // Strip DeepSeek-only fields that may confuse downstream
  delete out.reasoning_effort;
  // Keep `thinking` — moonshot-proxy strips it before hitting OpenAI
  // but Moonshot itself accepts the field.
  return out;
}

function callUpstream({ host, port, path, apiKey, body, useTls, onResponse, onError }) {
  const transport = useTls ? https : http;
  const req = transport.request(
    {
      host,
      port: port ?? (useTls ? 443 : 80),
      path,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        'Content-Length': Buffer.byteLength(body),
      },
      timeout: 180_000,
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
  const headers = { ...upstreamRes.headers };
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

    applyThinkingPolicy(payload);

    if (isCircuitOpen()) {
      log(`circuit open — routing to moonshot-proxy directly`);
      return forwardToFallback(payload, clientRes, 'circuit-open');
    }

    return tryDeepSeek(payload, clientRes);
  });
  clientReq.on('error', err => {
    log(`client request error: ${err.message}`);
  });
}

function tryDeepSeek(payload, clientRes) {
  if (!DEEPSEEK_API_KEY) {
    log('DEEPSEEK_API_KEY missing — failing over to moonshot-proxy');
    return forwardToFallback(payload, clientRes, 'no-deepseek-key');
  }
  const reqBody = JSON.stringify(payload);
  let upstreamHandled = false;
  callUpstream({
    host: DEEPSEEK_HOST,
    path: DEEPSEEK_PATH,
    apiKey: DEEPSEEK_API_KEY,
    body: reqBody,
    useTls: true,
    onResponse: upstreamRes => {
      const status = upstreamRes.statusCode ?? 0;
      if (status >= 200 && status < 300) {
        recentFailures = [];
        upstreamHandled = true;
        return streamResponse(upstreamRes, clientRes);
      }
      let errBody = '';
      upstreamRes.on('data', chunk => {
        errBody += chunk;
      });
      upstreamRes.on('end', () => {
        upstreamHandled = true;
        if (isOverloadError(status, errBody)) {
          log(`DeepSeek ${status} overload → failover to moonshot-proxy`);
          recordFailure();
          return forwardToFallback(payload, clientRes, `deepseek-${status}`);
        }
        log(`DeepSeek ${status} (non-overload) — passing through`);
        clientRes.writeHead(status, { 'Content-Type': 'application/json' });
        clientRes.end(errBody);
      });
    },
    onError: err => {
      if (upstreamHandled) return;
      upstreamHandled = true;
      log(`DeepSeek network error: ${err.message} → failover to moonshot-proxy`);
      recordFailure();
      forwardToFallback(payload, clientRes, `network-${err.message.slice(0, 30)}`);
    },
  });
}

function forwardToFallback(originalPayload, clientRes, reason) {
  const rewritten = rewriteForFallback(originalPayload);
  const reqBody = JSON.stringify(rewritten);
  let upstreamHandled = false;
  callUpstream({
    host: FALLBACK_HOST,
    port: FALLBACK_PROXY_PORT,
    path: FALLBACK_PATH,
    apiKey: '', // moonshot-proxy injects its own auth header
    body: reqBody,
    useTls: false,
    onResponse: upstreamRes => {
      upstreamHandled = true;
      const status = upstreamRes.statusCode ?? 0;
      log(`fallback proxy returned ${status} (reason=${reason})`);
      return streamResponse(upstreamRes, clientRes);
    },
    onError: err => {
      if (upstreamHandled) return;
      upstreamHandled = true;
      log(`fallback proxy network error: ${err.message}`);
      clientRes.writeHead(502, { 'Content-Type': 'application/json' });
      clientRes.end(
        JSON.stringify({
          error: {
            message: `proxy: both DeepSeek (${reason}) and moonshot-proxy failed: ${err.message}`,
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
  log(`listening on http://127.0.0.1:${PROXY_PORT}${DEEPSEEK_PATH}`);
  log(`thinking mode: ${THINKING_MODE}${REASONING_EFFORT ? ` (effort=${REASONING_EFFORT})` : ''}`);
  log(`fallback: moonshot-proxy at ${FALLBACK_HOST}:${FALLBACK_PROXY_PORT} (model=${FALLBACK_MODEL})`);
  if (!DEEPSEEK_API_KEY) log('WARN: DEEPSEEK_API_KEY not in env — every request will fall through to moonshot-proxy');
});

['SIGTERM', 'SIGINT'].forEach(sig => {
  process.on(sig, () => {
    log(`received ${sig} — shutting down`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 5000);
  });
});
