import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { lstat, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createClient } from "@connectrpc/connect";
import { resolveBindings } from "@cyclo/component/bindings";
import { Component, HealthStatus } from "@cyclo/component/contract";
import { parseDeclaration } from "@cyclo/component/declaration";
import {
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { createUnixTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";

import { runPassthrough } from "../src/main.mjs";
import { createPassthroughServer } from "../src/server.mjs";
import { createPassthroughServices } from "../src/services.mjs";
import { createUpstreamBinding } from "../src/upstream.mjs";

const declarationUrl = new URL("../component.conf", import.meta.url);

test("declares exactly one upstream Provider", async () => {
  const declaration = parseDeclaration(await readFile(declarationUrl, "utf8"));
  assert.deepEqual(declaration, {
    name: "passthrough",
    provides: [Component.typeName, Provider.typeName],
    requires: [{ name: "upstream", service: Provider.typeName }],
  });
});

test("forwards model, request payload, and response payloads exactly", async () => {
  const requestPayload = " { \"tools\": [{\"schema\": {\"anyOf\": []}}], \"x\": 1 } ";
  const responsePayloads = [
    "{\"type\":\"start\",\"partial\":{}}",
    " { \"type\": \"future_pi_event\", \"value\": \"unchanged\" } ",
  ];
  await withPassthrough({ responsePayloads }, async ({ provider, observed }) => {
    const actual = [];
    for await (const response of provider.infer(
      { model: "account/model", payload: requestPayload },
      { headers: { authorization: "Bearer caller-secret", cookie: "caller-secret" } },
    )) actual.push(response.payload);

    assert.equal(observed.requests.length, 1);
    assert.equal(observed.requests[0].model, "account/model");
    assert.equal(observed.requests[0].payload, requestPayload);
    assert.deepEqual(actual, responsePayloads);
    assert.equal(observed.headers[0].get("authorization"), null);
    assert.equal(observed.headers[0].get("cookie"), null);
  });
});

test("reports upstream health without leaking dependency errors", async () => {
  await withPassthrough({}, async ({ component, observed }) => {
    observed.failHealth = true;
    assert.deepEqual(await component.health({}), {
      $typeName: "cyclo.component.v1.HealthResponse",
      status: HealthStatus.NOT_READY,
      message: "upstream provider unavailable",
    });
    observed.failHealth = false;
    assert.equal((await component.health({})).status, HealthStatus.READY);
  });
});

test("propagates cancellation to the upstream without encoding it in JSON", async () => {
  const canceled = deferred();
  await withPassthrough({ wait: true, canceled }, async ({ provider }) => {
    const controller = new AbortController();
    const iterator = provider.infer(
      { model: "account/model", payload: "opaque" },
      { signal: controller.signal },
    )[Symbol.asyncIterator]();
    assert.equal((await iterator.next()).value.payload, "first");
    controller.abort();
    await assert.rejects(iterator.next());
    await withTimeout(canceled.promise);
  });
});

test("SIGTERM aborts active inference and removes the output socket", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-passthrough-signal-"));
  const upstreamSocket = join(directory, "upstream.sock");
  const outputSocket = join(directory, "output.sock");
  const canceled = deferred();
  const observed = state();
  const upstream = upstreamServer(observed, { wait: true, canceled });
  const signalSource = new EventEmitter();
  await listenComponentServer(upstream, { socketPath: upstreamSocket });
  const running = runPassthrough({
    env: {
      CYCLO_COMPONENT_SOCKET: outputSocket,
      CYCLO_REQUIRE_UPSTREAM_SOCKET: upstreamSocket,
    },
    signalSource,
  });

  try {
    await waitForSocket(outputSocket);
    const provider = createClient(Provider, createUnixTransport(outputSocket));
    const iterator = provider.infer({ model: "account/model", payload: "opaque" })[
      Symbol.asyncIterator
    ]();
    assert.equal((await iterator.next()).value.payload, "first");
    signalSource.emit("SIGTERM");
    await assert.rejects(iterator.next());
    await running;
    await withTimeout(canceled.promise);
    await assert.rejects(lstat(outputSocket), (error) => error?.code === "ENOENT");
  } finally {
    signalSource.emit("SIGTERM");
    await running.catch(() => {});
    await closeComponentServer(upstream);
    await rm(directory, { recursive: true, force: true });
  }
});

async function withPassthrough(options, run) {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-passthrough-"));
  const upstreamSocket = join(directory, "upstream.sock");
  const outputSocket = join(directory, "output.sock");
  const observed = state();
  const upstream = upstreamServer(observed, options);
  let passthrough;
  try {
    await listenComponentServer(upstream, { socketPath: upstreamSocket });
    const binding = createUpstreamBinding({
      env: { CYCLO_REQUIRE_UPSTREAM_SOCKET: upstreamSocket },
    });
    passthrough = await createPassthroughServer({
      services: createPassthroughServices({ upstream: binding, healthTimeoutMs: 100 }),
    });
    await listenComponentServer(passthrough, { socketPath: outputSocket });
    const transport = createUnixTransport(outputSocket);
    await run({
      component: createClient(Component, transport),
      provider: createClient(Provider, transport),
      observed,
    });
  } finally {
    if (passthrough) await closeComponentServer(passthrough).catch(() => {});
    await closeComponentServer(upstream).catch(() => {});
    await rm(directory, { recursive: true, force: true });
  }
}

function state() {
  return { failHealth: false, headers: [], requests: [] };
}

function upstreamServer(observed, { responsePayloads = [], wait = false, canceled } = {}) {
  const bindings = resolveBindings(parseDeclaration(`
    component upstream
    provide cyclo.component.v1.Component
    provide cyclo.provider.v1.Provider
  `), [Component, Provider]);
  return createComponentServer({
    bindings,
    implementations: new Map([
      [Component.typeName, { health: () => ({ status: HealthStatus.READY }) }],
      [Provider.typeName, {
        listModels(_request, context) {
          observed.headers.push(new Headers(context.requestHeader));
          if (observed.failHealth) throw new Error("private upstream failure");
          return { models: [] };
        },
        async *infer(request, context) {
          observed.headers.push(new Headers(context.requestHeader));
          observed.requests.push(request);
          if (wait) {
            yield { payload: "first" };
            try {
              await aborted(context.signal);
              throw context.signal.reason;
            } finally {
              canceled?.resolve();
            }
          }
          for (const payload of responsePayloads) yield { payload };
        },
      }],
    ]),
  });
}

function aborted(signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function withTimeout(promise, timeoutMs = 1_000) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timed out")), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
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
