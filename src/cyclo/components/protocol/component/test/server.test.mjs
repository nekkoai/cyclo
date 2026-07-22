import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { lstat, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { Component, HealthStatus } from "@cyclo/component/contract";

import { resolveBindings } from "../src/bindings.mjs";
import { parseDeclaration } from "../src/declaration.mjs";
import { checkComponentHealth } from "../src/health.mjs";
import {
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "../src/server.mjs";

test("replaces a stale socket and applies the requested mode", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-component-stale-"));
  const socketPath = join(directory, "component.sock");
  await leaveStaleSocket(socketPath);
  const server = healthServer();
  try {
    await listenComponentServer(server, { socketPath, mode: 0o620 });
    assert.equal((await lstat(socketPath)).mode & 0o777, 0o620);
    assert.equal(await checkComponentHealth({ socketPath }), true);
  } finally {
    await Promise.all([
      closeComponentServer(server),
      closeComponentServer(server),
    ]);
    await rm(directory, { recursive: true, force: true });
  }
});

test("never replaces a live socket or a non-socket path", async (t) => {
  await t.test("live socket", async () => {
    const directory = await mkdtemp(join(tmpdir(), "cyclo-component-live-"));
    const socketPath = join(directory, "component.sock");
    const occupant = createNetServer();
    const server = healthServer();
    try {
      await listenNet(occupant, socketPath);
      await assert.rejects(
        listenComponentServer(server, { socketPath }),
        (error) => error?.code === "EADDRINUSE",
      );
      assert.equal(occupant.listening, true);
    } finally {
      await closeComponentServer(server);
      await closeNet(occupant);
      await rm(directory, { recursive: true, force: true });
    }
  });

  await t.test("regular file", async () => {
    const directory = await mkdtemp(join(tmpdir(), "cyclo-component-file-"));
    const socketPath = join(directory, "component.sock");
    const server = healthServer();
    try {
      await writeFile(socketPath, "keep me", "utf8");
      await assert.rejects(
        listenComponentServer(server, { socketPath }),
        /refusing to replace non-socket path/u,
      );
      assert.equal(await readFile(socketPath, "utf8"), "keep me");
    } finally {
      await closeComponentServer(server);
      await rm(directory, { recursive: true, force: true });
    }
  });
});

test("a closed server can listen and close again without leaving a socket", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-component-relisten-"));
  const firstPath = join(directory, "first.sock");
  const secondPath = join(directory, "second.sock");
  const server = healthServer();
  try {
    await listenComponentServer(server, { socketPath: firstPath });
    await closeComponentServer(server);
    await assert.rejects(lstat(firstPath), (error) => error?.code === "ENOENT");

    await listenComponentServer(server, { socketPath: secondPath });
    assert.equal((await lstat(secondPath)).mode & 0o777, 0o666);
    assert.equal(await checkComponentHealth({ socketPath: secondPath }), true);
    await closeComponentServer(server);
    await assert.rejects(lstat(secondPath), (error) => error?.code === "ENOENT");
  } finally {
    await closeComponentServer(server);
    await rm(directory, { recursive: true, force: true });
  }
});

function healthServer() {
  const bindings = resolveBindings(
    parseDeclaration(`
      component test-health
      provide cyclo.component.v1.Component
    `),
    [Component],
  );
  return createComponentServer({
    bindings,
    implementations: new Map([[Component.typeName, {
      health() {
        return { status: HealthStatus.READY, message: "ready" };
      },
    }]]),
  });
}

async function leaveStaleSocket(socketPath) {
  const source = [
    'const { createServer } = require("node:net");',
    "const server = createServer();",
    'server.listen(process.argv[1], () => process.stdout.write("ready\\n"));',
    "setInterval(() => {}, 1000);",
  ].join("\n");
  const child = spawn(process.execPath, ["-e", source, socketPath], {
    stdio: ["ignore", "pipe", "inherit"],
  });
  await waitForLine(child, "ready");
  const exited = once(child, "exit");
  child.kill("SIGKILL");
  await exited;
}

function waitForLine(child, expected) {
  return new Promise((resolve, reject) => {
    let output = "";
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      reject(new Error(`socket helper exited before ready: ${signal ?? code}`));
    });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      output += chunk;
      if (output.split(/\r?\n/u).includes(expected)) resolve();
    });
  });
}

function listenNet(server, socketPath) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
}

function closeNet(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}
