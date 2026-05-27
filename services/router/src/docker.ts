import Dockerode from "dockerode";
import { spawn } from "child_process";
import { config } from "./config.js";
import { log } from "./log.js";
import type { Tenant } from "./db.js";

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
  timeoutMs = 60_000,
): Promise<void> {
  // Use docker CLI (via child_process) for the health probe instead of dockerode.
  // PR #23 tried to make dockerode work and failed: local reproduction showed
  // dockerode-mediated `c.exec({...}).start({hijack:true})` consistently times
  // out at 5s even though the underlying command completes in 1.5-2s. We
  // tested both with and without AttachStdout — neither was reliable.
  // docker CLI is what works (manually tested on the VPS) so we use that.
  //
  // Per-probe timeout: 12s (worst-case observed locally: 7.2s when the gateway
  // is in early startup; typical is <2s). Outer deadline: 60s (room for ~5
  // probe attempts including 1s sleep between).
  if (!tenant.container_name) {
    throw new Error(`tenant ${tenant.id} has no container_name`);
  }
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    const r = await dockerCliExec(
      tenant.container_name,
      ["openclaw", "gateway", "health"],
      12_000,
    );
    if (r.exitCode === 0) return;
    log.debug(
      { tenant: tenant.id, exit: r.exitCode, stderr: r.stderr.slice(0, 200) },
      "gateway health probe non-zero — will retry",
    );
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(
    `tenant ${tenant.id} did not become healthy within ${timeoutMs}ms (gateway health probe failed)`,
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

  // Pin to Haiku for cost. The "anthropic/" prefix IS required — without it,
  // OpenClaw defaults to the configured gateway provider ("openai/" on a fresh
  // install) and routes "claude-haiku-4-5" as "openai/claude-haiku-4-5", which
  // fails: `FailoverError: Unknown model: openai/claude-haiku-4-5`.
  const model = process.env.ARETE_AGENT_MODEL || "anthropic/claude-haiku-4-5";
  const exec = await c.exec({
    Cmd: [
      "openclaw",
      "agent",
      "--json",
      "--message",
      messageText,
      "--session-key",
      sessionKey,
      "--model",
      model,
      "--timeout",
      "90",
    ],
    AttachStdout: true,
    AttachStderr: true,
  });

  const stream = await exec.start({ Detach: false, hijack: true });

  let stdout = "";
  let stderr = "";
  await new Promise<void>((resolve, reject) => {
    // dockerode multiplexes stdout/stderr on a single stream; we use docker's
    // demuxStream helper to split them into two Writable buffers.
    const { PassThrough } = require("stream");
    const outBuf = new PassThrough();
    const errBuf = new PassThrough();
    outBuf.on("data", (d: Buffer) => (stdout += d.toString("utf8")));
    errBuf.on("data", (d: Buffer) => (stderr += d.toString("utf8")));
    docker.modem.demuxStream(stream, outBuf, errBuf);
    stream.on("end", () => resolve());
    stream.on("error", reject);
  });

  const info = await exec.inspect();
  if (info.ExitCode !== 0) {
    log.error(
      { tenant: tenant.id, exit: info.ExitCode, stderr: stderr.slice(0, 500) },
      "openclaw agent exited non-zero",
    );
    throw new Error(`openclaw agent exited ${info.ExitCode}: ${stderr.slice(0, 200)}`);
  }

  // openclaw agent --json shape (observed from openclaw@2026.5.22):
  //   {
  //     "runId": "...",
  //     "status": "ok",
  //     "summary": "completed",
  //     "result": {
  //       "payloads": [{ "text": "...the reply...", "mediaUrl": null }],
  //       "meta": { "sessionId": "...", "provider": "anthropic", "model": "...",
  //                 "durationMs": 5716, ... }
  //     }
  //   }
  // The reply text lives at result.payloads[0].text. There may be multiple
  // payloads for multi-part responses; we join them with newlines.
  let reply: string | undefined;
  let agentMeta: Record<string, unknown> | undefined;
  try {
    const obj = JSON.parse(stdout.trim());
    const payloads = obj?.result?.payloads;
    if (Array.isArray(payloads) && payloads.length > 0) {
      reply = payloads
        .map((p: { text?: string }) => p?.text)
        .filter((t): t is string => typeof t === "string" && t.length > 0)
        .join("\n\n");
    }
    agentMeta = obj?.result?.meta?.agentMeta;
    // Surface useful diagnostics in the journal: session id + duration + token
    // accounting if the agent reported them.
    if (agentMeta) {
      log.debug(
        {
          tenant: tenant.id,
          sessionId: agentMeta.sessionId,
          provider: agentMeta.provider,
          model: agentMeta.model,
          durationMs: obj?.result?.meta?.durationMs,
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
