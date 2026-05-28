import Dockerode from "dockerode";
import { spawn } from "child_process";
import { PassThrough } from "stream";
import { config } from "./config.js";
import { log } from "./log.js";
import { recordUsage, type Tenant } from "./db.js";

const docker = new Dockerode({ socketPath: config.DOCKER_SOCKET });

/**
 * Shell out to `docker exec` via child_process. Used by health probes (and
 * could be used by other exec paths) because dockerode's stream-event handling
 * adds 3-5s of overhead and was the root cause of PRs #23's incomplete fix —
 * `openclaw gateway health` runs in 1.5-2s via docker CLI but our dockerode-
 * based probe was timing out at 5s in every iteration. Local repro shows docker
 * CLI is consistently 1.5-7s; dockerode-mediated exec is consistently >5s.
 */
function dockerCliExec(
  containerName: string,
  cmd: string[],
  timeoutMs: number,
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const p = spawn("docker", ["exec", containerName, ...cmd], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    p.stdout.on("data", (d: Buffer) => (stdout += d.toString("utf8")));
    p.stderr.on("data", (d: Buffer) => (stderr += d.toString("utf8")));
    const killer = setTimeout(() => p.kill("SIGKILL"), timeoutMs);
    p.on("close", (code) => {
      clearTimeout(killer);
      resolve({ exitCode: code ?? -1, stdout, stderr });
    });
  });
}

export async function ensureRunning(tenant: Tenant): Promise<void> {
  if (!tenant.container_name) {
    throw new Error(
      `tenant ${tenant.id} has no container_name — Phase D provisioning incomplete`,
    );
  }
  const c = docker.getContainer(tenant.container_name);

  let info: Dockerode.ContainerInspectInfo;
  try {
    info = await c.inspect();
  } catch (err: any) {
    if (err?.statusCode === 404) {
      throw new Error(
        `container ${tenant.container_name} does not exist — provision-test-tenant.sh first`,
      );
    }
    throw err;
  }

  if (info.State.Running) return;

  log.info({ tenant: tenant.id, container: tenant.container_name }, "starting container");
  await c.start();
}

export async function stopContainer(name: string): Promise<void> {
  try {
    const c = docker.getContainer(name);
    await c.stop({ t: 10 }); // 10s grace
    log.info({ container: name }, "stopped container");
  } catch (err: any) {
    if (err?.statusCode === 304 || err?.statusCode === 404) return;
    throw err;
  }
}

export async function waitHealthy(
  tenant: Tenant,
  timeoutMs = 90_000,
): Promise<void> {
  // Two-tier readiness check:
  //   1. Cheap: docker inspect — container must be Running and not Restarting.
  //      If exited / restarting, the agent call will definitely fail, so we
  //      throw early.
  //   2. Probe: `openclaw gateway health` via docker CLI. If it passes within
  //      the deadline, the gateway is fully ready (best case).
  //
  // BUT: even when `openclaw gateway health` keeps timing out (the device
  // scope-upgrade pending-approval issue tracked in v2 backlog), the actual
  // `openclaw agent` calls work because they fall back to embedded mode.
  // So if the container is Running, we proceed anyway after a brief grace,
  // letting the agent call deal with whatever mode is available.
  //
  // This is intentionally lenient. See memory: arete-v2-backlog-openclaw-scopes
  // for the proper fix (pre-grant scopes via paired.json hot-patch).
  if (!tenant.container_name) {
    throw new Error(`tenant ${tenant.id} has no container_name`);
  }
  const c = docker.getContainer(tenant.container_name);
  const deadline = Date.now() + timeoutMs;
  const gracePeriodMs = 15_000; // ample for cold-start gateway init
  const startMs = Date.now();

  while (Date.now() < deadline) {
    // Cheap inspect — fail fast if container is in a bad state
    let info: Dockerode.ContainerInspectInfo;
    try {
      info = await c.inspect();
    } catch (err) {
      throw new Error(`tenant ${tenant.id} container inspect failed: ${String(err)}`);
    }
    if (!info.State.Running) {
      throw new Error(
        `tenant ${tenant.id} container not Running (state=${info.State.Status})`,
      );
    }
    // Probe gateway via docker CLI (5s budget per probe). If 0 → great, return.
    const r = await dockerCliExec(
      tenant.container_name,
      ["openclaw", "gateway", "health"],
      5_000,
    );
    if (r.exitCode === 0) {
      log.debug({ tenant: tenant.id }, "gateway health probe ok");
      return;
    }
    // Probe failed. If we've waited at least gracePeriodMs and container is
    // Running, assume gateway is responsive-enough-via-embedded-fallback and
    // let the agent call try. This is the workaround for the device scope
    // issue documented in memory: arete-v2-backlog-openclaw-scopes.
    if (Date.now() - startMs >= gracePeriodMs) {
      log.warn(
        { tenant: tenant.id, elapsed: Date.now() - startMs },
        "gateway health failing but container is Running — proceeding (agent may use embedded fallback)",
      );
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(
    `tenant ${tenant.id} did not become healthy within ${timeoutMs}ms (container not running)`,
  );
}

/**
 * Run a single agent turn inside the tenant container and return the reply text.
 *
 * Uses `docker exec arete-<id> openclaw agent --json -m "<text>" --session-key ...`
 * which writes a JSON object to stdout containing the assistant's reply. We don't
 * pass --deliver so OpenClaw doesn't try to talk to Telegram from inside the
 * container — the router relays the reply back to Telegram itself.
 */
export async function runAgentTurn(
  tenant: Tenant,
  messageText: string,
  telegramUserId: number,
): Promise<string> {
  const c = docker.getContainer(tenant.container_name!);
  // Per-user session key so memory is continuous across turns.
  const sessionKey = `agent:arete:tg-${telegramUserId}`;

  // Model selection: container's openclaw.json is configured with a primary
  // (DeepSeek V4 Pro by default per entrypoint.sh) plus fallbacks. Don't pass
  // --model at all — let OpenClaw use the configured primary + fallback chain.
  // Operators can still override per-call via ARETE_AGENT_MODEL.
  const explicitModel = process.env.ARETE_AGENT_MODEL;
  // CRITICAL: `--agent main` is what makes OpenClaw apply the
  // agents.list[0].identity = {name: "Arete"} from openclaw.json. Without
  // it, the CLI falls back to a generic default that prepends "You are a
  // personal assistant running inside OpenClaw" to the systemPrompt, and
  // the model identifies itself as "an AI assistant" instead of as Arete.
  //
  // Verified live 2026-05-28: identical message gets "I'm an AI assistant
  // running on OpenClaw" without --agent, and "Arete." with --agent main.
  const cmd = [
    "openclaw",
    "agent",
    "--json",
    "--agent",
    "main",
    "--message",
    messageText,
    "--session-key",
    sessionKey,
    "--timeout",
    "90",
  ];
  if (explicitModel) {
    cmd.push("--model", explicitModel);
  }
  // Use docker CLI (not dockerode) to avoid the same stream-hang bug that
  // forced waitHealthy off dockerode in PR #26. Live test 2026-05-28 proved
  // that manual `docker exec arete-... openclaw agent ...` returns in ~34s
  // with full usage data, but the dockerode-mediated version hangs > 60s
  // without ever resolving. Same exec primitive, completely different
  // reliability. We use spawn + 120s budget (covers worst-case DeepSeek
  // first-call init).
  const r = await dockerCliExec(tenant.container_name!, cmd, 120_000);
  const stdout = r.stdout;
  const stderr = r.stderr;
  // Synthesize an info-like object for downstream code that expected
  // dockerode's exec.inspect() return.
  const info = { ExitCode: r.exitCode };
  if (info.ExitCode !== 0) {
    log.error(
      { tenant: tenant.id, exit: info.ExitCode, stderr: stderr.slice(0, 500) },
      "openclaw agent exited non-zero",
    );
    // Record the failed turn so we have observability into agent error rates
    // per tenant. Best-effort; if recordUsage throws, log + continue.
    void recordUsage({
      tenant_id: tenant.id,
      ok: false,
      model: explicitModel ?? null,
      raw: { exit: info.ExitCode, stderr: stderr.slice(0, 500) },
    });
    throw new Error(`openclaw agent exited ${info.ExitCode}: ${stderr.slice(0, 200)}`);
  }

  // openclaw agent --json envelope (per docs.openclaw.ai/cli/agent + observed):
  //   Without --deliver (our case):
  //     { "payloads": [{ "text": "...", "mediaUrl": null }], "meta": { "durationMs": 1200 } }
  //   With --deliver (we don't use): adds deliveryStatus, result.deliveryStatus
  //   Observed in 2026-05-27 manual test against arete:local:
  //     { "runId", "status", "summary", "result": { "payloads": [...], "meta": {...} } }
  //
  // Both shapes can occur — handle both. Reply text lives at
  //   payloads[0].text          (documented, no --deliver)
  // OR
  //   result.payloads[0].text   (observed, fuller envelope)
  // Multi-part responses get joined with double-newline.
  let reply: string | undefined;
  let durationMs: unknown;
  let agentMeta: Record<string, unknown> | undefined;
  try {
    const obj = JSON.parse(stdout.trim());
    const payloads = obj?.payloads ?? obj?.result?.payloads;
    if (Array.isArray(payloads) && payloads.length > 0) {
      reply = payloads
        .map((p: { text?: string }) => p?.text)
        .filter((t): t is string => typeof t === "string" && t.length > 0)
        .join("\n\n");
    }
    durationMs = obj?.meta?.durationMs ?? obj?.result?.meta?.durationMs;
    agentMeta = obj?.result?.meta?.agentMeta ?? obj?.meta?.agentMeta;
    // Q-Q: write a usage event per agent turn. Best-effort (recordUsage
    // catches its own errors). Token counts come from agentMeta.tokens if
    // populated (varies by model/openclaw version); model + provider always
    // populated when the call succeeded.
    const tokens = (agentMeta?.tokens ?? obj?.result?.meta?.tokens) as
      | { input?: number; output?: number; cached?: number }
      | undefined;
    void recordUsage({
      tenant_id: tenant.id,
      ok: true,
      provider: (agentMeta?.provider as string) ?? null,
      model: (agentMeta?.model as string) ?? explicitModel ?? null,
      duration_ms: typeof durationMs === "number" ? durationMs : null,
      input_tokens: typeof tokens?.input === "number" ? tokens.input : null,
      output_tokens: typeof tokens?.output === "number" ? tokens.output : null,
      cached_tokens: typeof tokens?.cached === "number" ? tokens.cached : null,
      session_id: (agentMeta?.sessionId as string) ?? null,
      raw: agentMeta,
    });
    if (agentMeta || durationMs) {
      log.debug(
        {
          tenant: tenant.id,
          sessionId: agentMeta?.sessionId,
          provider: agentMeta?.provider,
          model: agentMeta?.model,
          durationMs,
        },
        "agent meta",
      );
    }
  } catch {
    // Not JSON (or partial). Fall back to whole stdout — at least the user
    // sees the raw error rather than total silence.
  }
  if (!reply || !reply.trim()) {
    reply = stdout.trim();
  }
  log.debug(
    { tenant: tenant.id, bytes: reply.length },
    "agent turn completed",
  );
  return reply;
}

export interface ContainerStats {
  name: string;
  state: string;
  status: string;
}

export async function listAreteContainers(): Promise<ContainerStats[]> {
  const list = await docker.listContainers({
    all: true,
    filters: { name: ["arete-"] },
  });
  return list.map((c) => ({
    name: c.Names[0]?.replace(/^\//, "") ?? "<unknown>",
    state: c.State,
    status: c.Status,
  }));
}
