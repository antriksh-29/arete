// Reproduces the exact dockerode hang that PR #23 fixes.
// Compares: AttachStdout=false (buggy) vs AttachStdout=true (fixed).
//
// Expected output:
//   - AttachStdout=false: hangs >10s, never resolves
//   - AttachStdout=true:  completes in ~2s
import Dockerode from "dockerode";

const docker = new Dockerode({
  socketPath: process.env.DOCKER_SOCKET || `${process.env.HOME}/.colima/default/docker.sock`,
});

async function setup() {
  // Pull alpine if needed, start a long-running container we can exec into
  console.log("[setup] pulling alpine...");
  await new Promise((resolve, reject) => {
    docker.pull("alpine:latest", (err, stream) => {
      if (err) return reject(err);
      docker.modem.followProgress(stream, (e) => (e ? reject(e) : resolve()));
    });
  });
  console.log("[setup] creating sleep container...");
  const c = await docker.createContainer({
    Image: "alpine:latest",
    Cmd: ["sleep", "300"],
    name: `arete-test-${Date.now()}`,
  });
  await c.start();
  return c;
}

async function execBuggy(c) {
  // Mimics the OLD waitHealthy implementation
  const start = Date.now();
  console.log("[buggy ] exec start, AttachStdout=false");
  const exec = await c.exec({
    Cmd: ["sh", "-c", "echo hello; sleep 2; echo bye"],
    AttachStdout: false,
    AttachStderr: false,
  });
  const stream = await exec.start({ Detach: false });
  console.log("[buggy ] waiting for stream.on('end')...");
  const result = await Promise.race([
    new Promise((resolve) => stream.on("end", () => resolve("end fired"))),
    new Promise((resolve) => setTimeout(() => resolve("TIMEOUT after 10s"), 10000)),
  ]);
  const elapsed = Date.now() - start;
  console.log(`[buggy ] result: ${result} (elapsed ${elapsed}ms)`);
  return result === "end fired";
}

async function execFixed(c) {
  // Mimics PR #23's new waitHealthy implementation
  const start = Date.now();
  console.log("[fixed ] exec start, AttachStdout=true + demux + 5s watchdog");
  const exec = await c.exec({
    Cmd: ["sh", "-c", "echo hello; sleep 2; echo bye"],
    AttachStdout: true,
    AttachStderr: true,
  });
  const stream = await exec.start({ Detach: false, hijack: true });
  const { PassThrough } = await import("stream");
  const sink = new PassThrough();
  sink.resume();
  docker.modem.demuxStream(stream, sink, sink);
  const result = await Promise.race([
    new Promise((resolve, reject) => {
      stream.on("end", () => resolve("end fired"));
      stream.on("error", reject);
    }),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("watchdog 5s")), 5000),
    ),
  ]).catch((e) => `error: ${e.message}`);
  const elapsed = Date.now() - start;
  console.log(`[fixed ] result: ${result} (elapsed ${elapsed}ms)`);
  return result === "end fired";
}

async function main() {
  const c = await setup();
  try {
    console.log("\n=== BUGGY VERSION (PR #17/22 code path) ===");
    const buggyOk = await execBuggy(c);
    console.log(`buggy "end" fired in time? ${buggyOk}`);

    console.log("\n=== FIXED VERSION (PR #23 code path) ===");
    const fixedOk = await execFixed(c);
    console.log(`fixed "end" fired in time? ${fixedOk}`);

    console.log("\n=== VERDICT ===");
    if (!buggyOk && fixedOk) {
      console.log("✓ Bug reproduced. ✓ PR #23 fix verified.");
      process.exit(0);
    } else if (buggyOk && fixedOk) {
      console.log("? Buggy version unexpectedly worked — colima/docker may behave differently than the VPS");
      process.exit(2);
    } else {
      console.log("✗ Fix did not work as expected");
      process.exit(1);
    }
  } finally {
    await c.stop({ t: 0 }).catch(() => {});
    await c.remove().catch(() => {});
  }
}

main().catch((err) => {
  console.error("test harness error:", err);
  process.exit(3);
});
