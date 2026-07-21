import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter, once } from "node:events";
import { lstat, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { Component, HealthStatus } from "@cyclo/component/contract";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { createUnixTransport } from "@cyclo/component/transport";
import { FinishReason, Provider } from "@cyclo/provider/contract";

import { checkGatewayHealth } from "../src/healthcheck.mjs";
import { runGateway } from "../src/main.mjs";
import { createGatewayServer } from "../src/server.mjs";

const healthcheckPath = fileURLToPath(new URL("../src/healthcheck.mjs", import.meta.url));

test("serves Component and Provider over one HTTP/1.1 Unix socket", async () => {
  await withGateway(async ({ socketPath, services }) => {
    const transport = gatewayTransport(socketPath);
    const component = createClient(Component, transport);
    const provider = createClient(Provider, transport);

    assert.equal((await component.health({})).status, HealthStatus.READY);
    assert.deepEqual((await provider.listModels({})).models, []);

    const events = [];
    for await (const response of provider.infer({ model: "test-model" })) {
      events.push(response.event.case);
    }
    assert.deepEqual(events, ["started", "finished"]);

    assert.equal(await checkGatewayHealth({ socketPath }), true);
    assert.equal(await healthcheckExit(socketPath), 0);
    services.status = HealthStatus.NOT_READY;
    assert.equal(await checkGatewayHealth({ socketPath }), false);
    assert.equal(await healthcheckExit(socketPath), 1);
  });
});

for (const signalName of ["SIGTERM", "SIGINT"]) {
  test(`${signalName} aborts active RPCs and removes the owned socket`, async () => {
    const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-signal-"));
    const socketPath = join(directory, "gateway.sock");
    const signalSource = new EventEmitter();
    const run = runGateway({
      services: fakeServices({ waitForCancellation: true }),
      env: { CYCLO_COMPONENT_SOCKET: socketPath },
      signalSource,
    });

    try {
      await waitForSocket(socketPath);
      const provider = createClient(Provider, gatewayTransport(socketPath));
      const iterator = provider.infer({ model: "test-model" })[Symbol.asyncIterator]();
      assert.equal((await iterator.next()).value.event.case, "started");

      signalSource.emit(signalName);
      await assert.rejects(
        iterator.next(),
        (error) => error instanceof ConnectError && error.code === Code.Unavailable,
      );
      await run;
      await assert.rejects(lstat(socketPath), (error) => error?.code === "ENOENT");
    } finally {
      signalSource.emit(signalName);
      await run.catch(() => {});
      await rm(directory, { recursive: true, force: true });
    }
  });
}

async function withGateway(run) {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-server-"));
  const socketPath = join(directory, "gateway.sock");
  const services = fakeServices();
  const server = await createGatewayServer({ services });
  try {
    await listenComponentServer(server, { socketPath });
    await run({ socketPath, services });
  } finally {
    await closeComponentServer(server);
    await rm(directory, { recursive: true, force: true });
  }
}

function fakeServices({ waitForCancellation = false } = {}) {
  const services = {
    status: HealthStatus.READY,
    component: {
      health() {
        return { status: services.status, message: "test" };
      },
    },
    provider: {
      listModels() {
        return { models: [] };
      },
      async *infer(request, context) {
        yield {
          event: {
            case: "started",
            value: { responseId: "response", model: request.model },
          },
        };
        if (waitForCancellation) {
          await aborted(context.signal);
          throw context.signal.reason;
        }
        yield {
          event: {
            case: "finished",
            value: { reason: FinishReason.STOP },
          },
        };
      },
    },
  };
  return services;
}

function gatewayTransport(socketPath) {
  return createUnixTransport(socketPath);
}

function aborted(signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
}

async function waitForSocket(socketPath) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await lstat(socketPath)).isSocket()) return;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`timed out waiting for ${socketPath}`);
}

function healthcheckExit(socketPath) {
  const child = spawn(process.execPath, [healthcheckPath], {
    env: { ...process.env, CYCLO_COMPONENT_SOCKET: socketPath },
    stdio: "ignore",
  });
  return once(child, "exit").then(([code]) => code);
}
