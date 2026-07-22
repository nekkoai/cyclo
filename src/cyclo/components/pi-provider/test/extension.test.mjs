import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { connectNodeAdapter } from "@connectrpc/connect-node";
import { Modality, Provider } from "@cyclo/provider/contract";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { groupModels } from "../src/adapter.mjs";
import { registerCycloProviders } from "../src/extension.mjs";

test("pins the Pi package to the inference format advertised on the wire", async () => {
  const manifest = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(
    PI_INFERENCE_FORMAT,
    `pi-ai@${manifest.peerDependencies["@earendil-works/pi-ai"]}`,
  );
  assert.equal(
    manifest.devDependencies["@earendil-works/pi-ai"],
    manifest.peerDependencies["@earendil-works/pi-ai"],
  );
});

test("sends one opaque Pi call frame and returns native Pi events unchanged", async () => {
  await withProvider(async ({ socketPath, state }) => {
    const registrations = new Map();
    assert.equal(await registerCycloProviders({
      registerProvider(name, configuration) { registrations.set(name, configuration); },
    }, { socketPath }), 2);

    const registration = registrations.get("work");
    const selected = selectedModel(registration, "model-a");
    const context = {
      systemPrompt: "Be exact.",
      tools: [{
        name: "lens_diagnostics",
        description: "No Cyclo schema policy applies",
        parameters: {
          type: "object",
          properties: {
            refreshRunners: {
              anyOf: [{ type: "boolean" }, { type: "string", enum: ["always", "never"] }],
            },
          },
          "x-future-keyword": { nested: true },
        },
      }],
      messages: [{
        role: "assistant",
        content: [{ type: "text", text: "opaque", textSignature: "signed-history" }],
        api: "future-api",
        provider: "future-provider",
        model: "future-model",
        usage: zeroUsage(),
        stopReason: "stop",
        timestamp: 1,
      }],
      futureContextField: { preserved: true },
    };
    const options = {
      reasoning: "high",
      futureProviderOption: { preserved: true },
      metadata: { safe: "metadata" },
      apiKey: "must-not-cross",
      headers: { authorization: "must-not-cross" },
      env: { SECRET: "must-not-cross" },
      transport: "websocket",
      timeoutMs: 12_345,
      websocketConnectTimeoutMs: 234,
      maxRetries: 9,
      maxRetryDelayMs: 999_999,
    };

    const events = [];
    for await (const event of registration.streamSimple(selected, context, options)) {
      events.push(event);
    }

    assert.equal(state.requests.length, 1);
    const request = state.requests[0];
    assert.equal(request.model, "work/model-a");
    const frame = JSON.parse(request.payload);
    assert.deepEqual(frame.context, context);
    assert.deepEqual(frame.options, {
      reasoning: "high",
      futureProviderOption: { preserved: true },
      metadata: { safe: "metadata" },
    });
    assert.doesNotMatch(request.payload, /must-not-cross/u);
    assert.equal(frame.options.transport, undefined);
    assert.equal(frame.options.timeoutMs, undefined);
    assert.equal(frame.options.websocketConnectTimeoutMs, undefined);
    assert.equal(frame.options.maxRetries, undefined);
    assert.equal(frame.options.maxRetryDelayMs, undefined);
    assert.deepEqual(events, successfulEvents(selected));
  });
});

test("propagates cancellation through ConnectRPC", async () => {
  await withProvider(async ({ socketPath, state }) => {
    const registration = await oneRegistration(socketPath);
    const controller = new AbortController();
    const iterator = registration.streamSimple(
      selectedModel(registration, "model-a"),
      { messages: [{ role: "user", content: "cancel", timestamp: 0 }] },
      { signal: controller.signal },
    )[Symbol.asyncIterator]();

    assert.equal((await iterator.next()).value.type, "start");
    controller.abort();
    const terminal = await withTimeout(iterator.next());
    assert.equal(terminal.value.type, "error");
    assert.equal(terminal.value.reason, "aborted");
    await withTimeout(state.canceled.promise);
  });
});

test("turns transport and JSON failures into Pi error terminals", async () => {
  await withProvider(async ({ socketPath }) => {
    const registration = await oneRegistration(socketPath);
    const events = [];
    for await (const event of registration.streamSimple(
      selectedModel(registration, "model-a"),
      { messages: [{ role: "user", content: "invalid-json", timestamp: 0 }] },
      {},
    )) events.push(event);
    assert.deepEqual(events.map(({ type }) => type), ["error"]);
    assert.match(events[0].error.errorMessage, /^Cyclo provider request failed:/u);
  });
});

test("rejects incompatible or unsafe catalogue entries", () => {
  assert.throws(
    () => groupModels([{ ...portableModel("work/model"), inferenceFormat: "other" }]),
    /unsupported inference format/u,
  );
  assert.throws(() => groupModels([portableModel("gateway/model")]), /PROVIDER\/MODEL/u);
  assert.throws(
    () => groupModels([portableModel("work/model"), portableModel("work/model")]),
    /duplicate/u,
  );
});

async function withProvider(run) {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-pi-provider-"));
  const socketPath = join(directory, "provider.sock");
  const state = { requests: [], canceled: deferred() };
  const server = createServer(connectNodeAdapter({
    connect: true,
    grpc: false,
    grpcWeb: false,
    routes(router) { router.service(Provider, implementation(state)); },
  }));
  try {
    await listen(server, socketPath);
    await run({ socketPath, state });
  } finally {
    await close(server);
    await rm(directory, { recursive: true, force: true });
  }
}

function implementation(state) {
  return {
    listModels() {
      return { models: [portableModel("other/model-b"), portableModel("work/model-a")] };
    },
    async *infer(request, context) {
      state.requests.push(request);
      const frame = JSON.parse(request.payload);
      const command = frame.context.messages.at(-1)?.content;
      if (command === "invalid-json") {
        yield { payload: "not-json" };
        return;
      }
      if (command === "cancel") {
        yield { payload: JSON.stringify(startEvent(request.model)) };
        try {
          await aborted(context.signal);
          throw context.signal.reason;
        } finally {
          state.canceled.resolve();
        }
      }
      for (const event of successfulEvents({ provider: "work", id: "model-a" })) {
        yield { payload: JSON.stringify(event) };
      }
    },
  };
}

function portableModel(id) {
  return {
    id,
    displayName: id,
    capabilities: {
      inputModalities: [Modality.TEXT, Modality.IMAGE],
      outputModalities: [Modality.TEXT],
      functionTools: true,
      parallelToolCalls: true,
      reasoning: true,
      extensionTypes: [],
    },
    contextWindowTokens: 128_000n,
    maxOutputTokens: 4_096n,
    inferenceFormat: PI_INFERENCE_FORMAT,
  };
}

function successfulEvents(model) {
  const partial = assistant(model);
  const complete = { ...partial, content: [{ type: "text", text: "native output" }] };
  return [
    { type: "start", partial },
    { type: "text_start", contentIndex: 0, partial },
    { type: "text_delta", contentIndex: 0, delta: "native output", partial: complete },
    { type: "text_end", contentIndex: 0, content: "native output", partial: complete },
    { type: "done", reason: "stop", message: complete },
  ];
}

function startEvent(model) {
  return { type: "start", partial: assistant({ provider: "work", id: model }) };
}

function assistant(model) {
  return {
    role: "assistant",
    content: [],
    api: "cyclo-pi",
    provider: model.provider,
    model: model.id,
    usage: zeroUsage(),
    stopReason: "stop",
    timestamp: 1,
  };
}

function zeroUsage() {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

async function oneRegistration(socketPath) {
  const registrations = new Map();
  await registerCycloProviders({
    registerProvider(name, configuration) { registrations.set(name, configuration); },
  }, { socketPath });
  return registrations.get("work");
}

function selectedModel(registration, id) {
  return { ...registration.models.find((model) => model.id === id), provider: "work" };
}

function listen(server, path) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(path, () => { server.off("error", reject); resolve(); });
  });
}

function close(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
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

function withTimeout(promise, milliseconds = 2_000) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("timed out")), milliseconds); }),
  ]).finally(() => clearTimeout(timer));
}
