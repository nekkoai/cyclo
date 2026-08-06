import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { Component, HealthStatus } from "@cyclo/component/contract";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { createDockerTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";

import { checkGatewayHealth } from "../src/healthcheck.mjs";
import { runGateway } from "../src/main.mjs";
import { createGatewayServer } from "../src/server.mjs";

test("serves Component and Provider over one HTTP/1.1 TCP port", async () => {
  await withGateway(async ({ target, services }) => {
    const transport = createDockerTransport(target);
    const component = createClient(Component, transport);
    const provider = createClient(Provider, transport);

    assert.equal((await component.health({})).status, HealthStatus.READY);
    assert.deepEqual((await provider.listModels({})).models, []);

    const payloads = [];
    for await (const response of provider.infer({
      model: "test-model",
      payload: "request",
    })) {
      payloads.push(response.payload);
    }
    assert.deepEqual(payloads, ["start", "done"]);

    assert.equal(await checkGatewayHealth({ target }), true);
    services.status = HealthStatus.NOT_READY;
    assert.equal(await checkGatewayHealth({ target }), false);
  });
});

for (const signalName of ["SIGTERM", "SIGINT"]) {
  test(`${signalName} aborts active RPCs and closes the listener`, async () => {
    const signalSource = new EventEmitter();
    const listening = deferred();
    const running = runGateway({
      services: fakeServices({ waitForCancellation: true }),
      signalSource,
      listenOptions: { host: "127.0.0.1", port: 0 },
      onListening: listening.resolve,
    });
    const address = await listening.promise;
    const target = `dns:///127.0.0.1:${address.port}`;

    try {
      const provider = createClient(Provider, createDockerTransport(target));
      const iterator = provider.infer({
        model: "test-model",
        payload: "request",
      })[Symbol.asyncIterator]();
      assert.equal((await iterator.next()).value.payload, "start");

      signalSource.emit(signalName);
      await assert.rejects(
        iterator.next(),
        (error) => error instanceof ConnectError && error.code === Code.Unavailable,
      );
      await running;
      await assert.rejects(checkGatewayHealth({ target, timeoutMs: 50 }));
    } finally {
      signalSource.emit(signalName);
      await running.catch(() => {});
    }
  });
}

async function withGateway(run) {
  const services = fakeServices();
  const server = await createGatewayServer({ services });
  try {
    const address = await listenComponentServer(server, {
      host: "127.0.0.1",
      port: 0,
    });
    await run({
      target: `dns:///127.0.0.1:${address.port}`,
      services,
    });
  } finally {
    await closeComponentServer(server);
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
      async *infer(_request, context) {
        yield { payload: "start" };
        if (waitForCancellation) {
          await aborted(context.signal);
          throw context.signal.reason;
        }
        yield { payload: "done" };
      },
    },
  };
  return services;
}

function aborted(signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    signal.addEventListener("abort", resolve, { once: true });
  });
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
