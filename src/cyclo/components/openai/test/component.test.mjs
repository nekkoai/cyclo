import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
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
import { createDockerTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";
import { encodePayload } from "@cyclo/provider/protocol";
import OpenAI from "openai";

import { main, runOpenAI } from "../src/main.mjs";
import { deferred, providerModel, textEvents } from "./helpers.mjs";

const declarationUrl = new URL("../component.conf", import.meta.url);

test("is declared as an independent terminal component with one Provider input", async () => {
  const declaration = parseDeclaration(await readFile(declarationUrl, "utf8"));
  assert.deepEqual(declaration, {
    name: "openai",
    provides: [Component.typeName],
    requires: [{ name: "provider", service: Provider.typeName }],
  });
});

test("runs its own health and OpenAI listeners over a real Provider link", async () => {
  const observed = { headers: [], requests: [], failList: false };
  const upstream = providerServer(observed);
  const signalSource = new EventEmitter();
  const listening = deferred();
  const upstreamTarget = await listenTarget(upstream);
  const running = runOpenAI({
    env: {
      DCOMP_LINK_PROVIDER: upstreamTarget,
      CYCLO_OPENAI_API_KEY: "edge-secret",
    },
    signalSource,
    componentListenOptions: { host: "127.0.0.1", port: 0 },
    httpListenOptions: { host: "127.0.0.1", port: 0 },
    onListening: listening.resolve,
  });

  try {
    const addresses = await withTimeout(listening.promise);
    const componentTarget = `dns:///127.0.0.1:${addresses.component.port}`;
    const component = createClient(Component, createDockerTransport(componentTarget));
    assert.deepEqual(await component.health({}), {
      $typeName: "cyclo.component.v1.HealthResponse",
      status: HealthStatus.READY,
      message: "ready",
    });

    const openai = new OpenAI({
      apiKey: "edge-secret",
      baseURL: `http://127.0.0.1:${addresses.openai.port}/v1`,
      maxRetries: 0,
    });
    const response = await openai.responses.create({
      model: "work/test-model",
      input: "component path",
      store: false,
    });
    assert.equal(response.output_text, "component output");
    assert.equal(observed.requests.length, 1);
    assert.equal(observed.requests[0].model, "work/test-model");
    assert.equal(observed.headers.at(-1).get("authorization"), null);

    observed.failList = true;
    assert.equal((await component.health({})).status, HealthStatus.NOT_READY);

    signalSource.emit("SIGTERM");
    await withTimeout(running);
    await assert.rejects(fetch(`http://127.0.0.1:${addresses.openai.port}/v1/models`));
  } finally {
    signalSource.emit("SIGTERM");
    await running.catch(() => {});
    await closeComponentServer(upstream).catch(() => {});
  }
});

test("requires its Provider link and accepts no command arguments", async () => {
  await assert.rejects(
    runOpenAI({ env: {}, signalSource: new EventEmitter() }),
    /DCOMP_LINK_PROVIDER is required/u,
  );
  await assert.rejects(
    runOpenAI({
      env: {
        DCOMP_LINK_PROVIDER: "dns:///provider:50051",
        CYCLO_OPENAI_API_KEY: "",
      },
      signalSource: new EventEmitter(),
      createProvider: () => ({
        client: {},
        callOptions() { return {}; },
      }),
      createServices: () => ({ component: {} }),
      createComponentServer: async () => ({ close() {} }),
    }),
    /CYCLO_OPENAI_API_KEY must be a non-empty string/u,
  );
  await assert.rejects(main(["serve"]), /usage: cyclo-openai-component/u);
});

function providerServer(observed) {
  const bindings = resolveBindings(parseDeclaration(`
    component fixture
    provide cyclo.component.v1.Component
    provide cyclo.provider.v1.Provider
  `), [Component, Provider]);
  return createComponentServer({
    bindings,
    implementations: new Map([
      [Component.typeName, {
        health() { return { status: HealthStatus.READY, message: "ready" }; },
      }],
      [Provider.typeName, {
        listModels(_request, context) {
          observed.headers.push(new Headers(context.requestHeader));
          if (observed.failList) throw new Error("private fixture failure");
          return { models: [providerModel()] };
        },
        async *infer(request, context) {
          observed.headers.push(new Headers(context.requestHeader));
          observed.requests.push(request);
          const events = textEvents("component output");
          for (const event of events) yield { payload: encodePayload(event) };
        },
      }],
    ]),
  });
}

async function listenTarget(server) {
  const address = await listenComponentServer(server, {
    host: "127.0.0.1",
    port: 0,
  });
  return `dns:///127.0.0.1:${address.port}`;
}

function withTimeout(promise, timeoutMs = 2_000) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error("timed out")), timeoutMs).unref();
    }),
  ]);
}
