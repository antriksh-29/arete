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
  timeoutMs = 30_000,
): Promise<void> {
  if (!tenant.internal_port) {
    throw new Error(`tenant ${tenant.id} has no internal_port`);
  }
  const url = `http://127.0.0.1:${tenant.internal_port}/health`;
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2_000) });
      if (res.ok) return;
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`tenant ${tenant.id} did not become healthy within ${timeoutMs}ms`);
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
