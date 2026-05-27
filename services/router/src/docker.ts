import Dockerode from "dockerode";
import { config } from "./config.js";
import { log } from "./log.js";
import type { Tenant } from "./db.js";

const docker = new Dockerode({ socketPath: config.DOCKER_SOCKET });

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
  // The container exposes a WebSocket Gateway on loopback (default port 18789).
  // Probe it from inside the container via `openclaw gateway health` rather than
  // hitting HTTP — the previous /health endpoint never existed.
  const c = docker.getContainer(tenant.container_name!);
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    try {
      const exec = await c.exec({
        Cmd: ["openclaw", "gateway", "health"],
        AttachStdout: false,
        AttachStderr: false,
      });
      const stream = await exec.start({ Detach: false });
      await new Promise<void>((resolve) => stream.on("end", () => resolve()));
      const info = await exec.inspect();
      if (info.ExitCode === 0) return;
    } catch {
      // container may not be up yet; retry
    }
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

  // Pin to Haiku for cost. Override per-call later via tenant config if needed.
  const model = process.env.ARETE_AGENT_MODEL || "claude-haiku-4-5";
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

  // The reply field name isn't documented; try common shapes in order.
  // First valid string we find wins; the rest fall through to stdout-as-text.
  let reply: string | undefined;
  try {
    const obj = JSON.parse(stdout.trim());
    reply =
      obj.reply ??
      obj.text ??
      obj.message ??
      obj.content ??
      obj.output ??
      obj.response ??
      (typeof obj.result === "string" ? obj.result : obj.result?.text);
  } catch {
    // Not JSON (or partial). Fall back to whole stdout.
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
