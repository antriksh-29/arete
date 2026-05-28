#!/usr/bin/env node
// arete-remind — schedule a reminder via the router's internal API.
//
// Container-side replacement for Alfred's `schedule-reminder.sh`. Talks to
// the router over the docker bridge; auth via the per-tenant INTERNAL_KEY
// the provisioner injected into our env at container creation.
//
// Usage:
//   arete-remind <time-spec> "<message>"
//
// Time spec:
//   "30m" "1h" "2h30m" "45s"            relative
//   "2026-05-29T03:30:00Z"              absolute ISO-8601 UTC
//
// The skill is responsible for resolving anything user-friendly ("tomorrow
// 9am", "next Monday") into one of those two forms — it has the tenant's
// timezone from USER.md to do that.

const ROUTER_URL = process.env.ROUTER_INTERNAL_URL;
const KEY = process.env.INTERNAL_KEY;

function die(msg, code = 1) {
  console.error(`arete-remind: ${msg}`);
  process.exit(code);
}

if (!ROUTER_URL) die("ROUTER_INTERNAL_URL not set in container env");
if (!KEY) die("INTERNAL_KEY not set in container env");

const [, , timeSpec, ...rest] = process.argv;
const message = rest.join(" ").trim();
if (!timeSpec || !message) {
  die("usage: arete-remind <time-spec> \"<message>\"", 64);
}
if (message.length > 4096) die("message exceeds 4096 chars", 64);

// Parse the time spec into an absolute UTC instant.
function resolveTime(spec) {
  // Absolute ISO-8601 — must end with Z (UTC). Skill computes from tenant tz.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$/.test(spec)) {
    const t = new Date(spec);
    if (Number.isNaN(t.getTime())) die(`invalid ISO timestamp: ${spec}`);
    return t;
  }
  // Relative — combinations of Nh / Nm / Ns
  const re = /(\d+)([hms])/g;
  let totalMs = 0;
  let matched = false;
  let m;
  // Reject if any non-matching characters are present.
  const stripped = spec.replace(re, "");
  while ((m = re.exec(spec)) !== null) {
    matched = true;
    const n = parseInt(m[1], 10);
    if (m[2] === "h") totalMs += n * 3600_000;
    else if (m[2] === "m") totalMs += n * 60_000;
    else totalMs += n * 1000;
  }
  if (!matched || stripped.trim() !== "") {
    die(`unrecognised time-spec: ${spec}`);
  }
  if (totalMs < 1000) die("reminders must be at least 1s in the future");
  if (totalMs > 365 * 24 * 3600_000) die("reminders cannot be > 1 year out");
  return new Date(Date.now() + totalMs);
}

const when = resolveTime(timeSpec);

const body = {
  message,
  scheduled_for_utc: when.toISOString(),
};

const res = await fetch(`${ROUTER_URL.replace(/\/+$/, "")}/internal/reminders/schedule`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Tenant-Key": KEY,
  },
  body: JSON.stringify(body),
  signal: AbortSignal.timeout(10_000),
}).catch((err) => die(`router unreachable: ${err.message}`, 2));

const text = await res.text();
let json = {};
try { json = JSON.parse(text); } catch { /* leave empty */ }

if (!res.ok || json.ok === false) {
  die(`schedule failed (${res.status}): ${text.slice(0, 300)}`, 2);
}

// Friendly confirmation for the agent to echo back verbatim.
const isoLocal = when.toISOString();
console.log(`✓ Reminder set for ${isoLocal} (UTC): ${message}`);
console.log(`  id=${json.id}`);
