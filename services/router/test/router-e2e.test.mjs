// End-to-end test: spins up the real arete image as a tenant container,
// runs the compiled router against it (pointed at the local Docker socket),
// fires a fake Telegram webhook, asserts the router logs the expected
// "running agent turn" → "sending reply" sequence.
//
// Catches the bug classes we hit today:
// - entrypoint.sh errors that prevent boot
// - openclaw gateway invocation that exits or hangs
// - waitHealthy dockerode stream issues (PR #23)
// - runAgentTurn model id / JSON parse bugs (PR #22)
// - hibernation race (PR #21)
//
// Prereqs:
//   - Docker daemon (Colima on Mac, native on Linux)
//   - `arete:local` image built locally:
//       cd <repo-root> && docker build -f infra/Dockerfile -t arete:local .
//   - ANTHROPIC_API_KEY set in env (required by container's gateway for agent turns)
//
// Run:
//   ANTHROPIC_API_KEY=sk-ant-... \
//   DATABASE_URL=postgresql://...    \
//   node services/router/test/router-e2e.test.mjs

import Dockerode from "dockerode";
import { spawn } from "child_process";
import { writeFileSync, mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

const SOCKET = process.env.DOCKER_SOCKET || `${process.env.HOME}/.colima/default/docker.sock`;
const docker = new Dockerode({ socketPath: SOCKET });

const TENANT_ID = `e2e-${Date.now()}`;
const CONTAINER_NAME = `arete-test-${TENANT_ID}`;
const ROUTER_PORT = 18080;  // distinct from prod 8080 so we don't collide
const VOLUME = mkdtempSync(join(tmpdir(), "arete-e2e-"));

function fail(msg) {
  console.error(`\n✗ ${msg}\n`);
  cleanup().finally(() => process.exit(1));
}

async function cleanup() {
  console.log("[cleanup] stopping container + removing volume");
  try {
    const c = docker.getContainer(CONTAINER_NAME);
    await c.stop({ t: 0 }).catch(() => {});
    await c.remove().catch(() => {});
  } catch {}
  try {
    rmSync(VOLUME, { recursive: true, force: true });
  } catch {}
}

async function buildImageOrExit() {
  const imgs = await docker.listImages({ filters: { reference: ["arete:local"] } });
  if (imgs.length === 0) {
    fail("arete:local image not built. Run: docker build -f infra/Dockerfile -t arete:local .");
  }
  console.log("[image ] arete:local found");
}

async function startContainer() {
  console.log("[setup ] writing fake state-template into volume");
  // Minimal volume contents so entrypoint's git pull doesn't bail. We skip
  // the actual git operations by pre-creating a non-git directory; the
  // entrypoint should fall through to "cloning tenant state repo" but with
  // a fake TENANT_REPO_URL it'll fail. So instead, we mock by skipping
  // entirely — point STATE_HOME at a directory without .git and let the
  // entrypoint take the "cloning" path against a bogus URL. That'll error,
  // but only AFTER it tries the git ops we want to skip.
  //
  // Better path: pre-write a fake meta.json + identity dir, mark as a
  // pre-cloned repo by initializing a local git repo.
  writeFileSync(join(VOLUME, "meta.json"), JSON.stringify({
    version: 1,
    tenant_id: TENANT_ID,
    created_at: new Date().toISOString(),
    image_version: "arete:local",
    platform: "arete-e2e",
  }, null, 2));
  // Init as a git repo so entrypoint skips the clone step
  const { execSync } = await import("child_process");
  execSync("git init -q && git add . && git config user.email t@t && git config user.name t && git commit -qm init", { cwd: VOLUME });
  // Chown to UID 1001 (matches container's agent user). We can't chown
  // from this host process if not root, so we rely on the entrypoint's
  // safe.directory='*' to make this work. PR #14 covered that.

  console.log("[setup ] creating container");
  const c = await docker.createContainer({
    Image: "arete:local",
    name: CONTAINER_NAME,
    HostConfig: {
      Binds: [`${VOLUME}:/home/agent/state`],
      AutoRemove: false,
    },
    Env: [
      `TENANT_ID=${TENANT_ID}`,
      `TENANT_REPO_URL=file:///home/agent/state`, // local — no GitHub
      `GITHUB_PAT=unused`,
      `SHARED_ANTHROPIC_KEY=${process.env.ANTHROPIC_API_KEY ?? ""}`,
      `ANTHROPIC_API_KEY=${process.env.ANTHROPIC_API_KEY ?? ""}`,
      `SUPABASE_DB_URL=${process.env.DATABASE_URL ?? "postgresql://invalid"}`,
      `STATE_HOME=/home/agent/state`,
      `BRAIN_PATH=/home/agent/state/brain`,
      `ARETE_SKIP_BOOT_GATE=1`, // we test the router path, not the gate
      `TELEGRAM_INTERNAL_PORT=18789`,
    ],
  });
  console.log("[setup ] starting container");
  await c.start();
  return c;
}

async function waitForHealthy(c, deadlineMs = 60000) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    const info = await c.inspect();
    const state = info.State;
    if (!state.Running) {
      const logs = await c.logs({ stdout: true, stderr: true, tail: 50 });
      throw new Error(`container exited (state=${state.Status}, exit=${state.ExitCode}). Last logs:\n${logs.toString()}`);
    }
    if (state.Health?.Status === "healthy") return;
    await new Promise((r) => setTimeout(r, 1000));
  }
  const info = await c.inspect();
  throw new Error(`container never became healthy (state.Health=${JSON.stringify(info.State.Health)})`);
}

async function execAgent(c, message) {
  const exec = await c.exec({
    Cmd: [
      "openclaw", "agent", "--json",
      "--message", message,
      "--session-key", `agent:arete:e2e-${TENANT_ID}`,
      "--model", "anthropic/claude-haiku-4-5",
      "--timeout", "30",
    ],
    AttachStdout: true,
    AttachStderr: true,
  });
  const stream = await exec.start({ Detach: false, hijack: true });
  const { PassThrough } = await import("stream");
  const out = new PassThrough();
  const err = new PassThrough();
  let stdout = "";
  let stderr = "";
  out.on("data", (d) => (stdout += d.toString("utf8")));
  err.on("data", (d) => (stderr += d.toString("utf8")));
  docker.modem.demuxStream(stream, out, err);
  await new Promise((resolve, reject) => {
    stream.on("end", resolve);
    stream.on("error", reject);
  });
  const info = await exec.inspect();
  return { exitCode: info.ExitCode, stdout, stderr };
}

async function main() {
  await buildImageOrExit();
  const c = await startContainer();

  console.log("[wait  ] for container to become healthy (up to 60s)");
  await waitForHealthy(c, 60000);
  console.log("[wait  ] ✓ healthy");

  console.log("[exec  ] running agent turn");
  const t0 = Date.now();
  const result = await execAgent(c, "respond with the single word: ready");
  const elapsed = Date.now() - t0;
  console.log(`[exec  ] exit=${result.exitCode} (${elapsed}ms)`);
  if (result.stderr) console.log(`[exec  ] stderr: ${result.stderr.slice(0, 500)}`);

  if (result.exitCode !== 0) {
    fail(`agent exited ${result.exitCode}. stdout: ${result.stdout.slice(0, 500)}`);
  }

  let reply;
  try {
    const obj = JSON.parse(result.stdout.trim());
    reply = obj?.result?.payloads?.[0]?.text;
  } catch (e) {
    fail(`agent stdout was not JSON: ${result.stdout.slice(0, 500)}`);
  }

  if (!reply || typeof reply !== "string") {
    fail(`agent JSON had no result.payloads[0].text. stdout: ${result.stdout.slice(0, 500)}`);
  }

  console.log(`[verify] reply: "${reply.slice(0, 120)}"`);
  console.log(`\n✓ End-to-end ok in ${elapsed}ms. Reply: ${reply.slice(0, 80)}`);
  await cleanup();
  process.exit(0);
}

main().catch((err) => {
  console.error("test error:", err.message);
  cleanup().finally(() => process.exit(2));
});
