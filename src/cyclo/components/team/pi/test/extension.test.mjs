import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import test from "node:test";

import { Code, ConnectError } from "@connectrpc/connect";
import { connectNodeAdapter } from "@connectrpc/connect-node";
import { Modality, Provider, ResourceExhaustionSchema } from "@cyclo/provider/contract";
import { createResourceExhaustedError } from "@cyclo/provider/errors";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { createPiAdapter } from "../../../gateway/src/pi-adapter.mjs";
import { createGatewayServices } from "../../../gateway/src/services.mjs";
import { groupModels, streamProvider } from "../src/adapter.mjs";
import { providerClient, registerCycloProviders } from "../src/extension.mjs";

const MODEL_ID_CASES = JSON.parse(await readFile(
  new URL("../../../protocol/provider/test/model-id-cases.json", import.meta.url),
  "utf8",
));

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

test("uses the shared Pi route-ID semantics", () => {
  for (const fixture of MODEL_ID_CASES) {
    const id = fixture.prefix + fixture.unit.repeat(fixture.repeat) + fixture.suffix;
    const warnings = [];
    const groups = groupModels([portableModel(id)], {
      onInvalid(message) { warnings.push(message); },
    });
    const accepted = [...groups.values()].flat().some((route) => route.publicId === id);

    assert.equal(accepted, fixture.valid, fixture.name);
    assert.equal(warnings.length, fixture.valid ? 0 : 1, fixture.name);
  }
});

test("sends one opaque Pi call frame and returns native Pi events unchanged", async () => {
  await withProvider(async ({ target, state }) => {
    const registrations = new Map();
    assert.equal(await registerCycloProviders({
      registerProvider(name, configuration) { registrations.set(name, configuration); },
    }, { target }), 2);

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

test("turns a gateway audit failure into a Pi error before done", async () => {
  const model = portableModel("work/model-a");
  const nativeEvents = successfulEvents({ provider: "work", id: "model-a" });
  let auditAttempts = 0;
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.assign(Object.create(null), {
        [model.id]: {
          publicModel: model,
          rawModel: {
            id: "model-a",
            provider: "openai",
            api: "openai-responses",
          },
        },
      }),
    },
    credentials: { async resolve() { return { apiKey: "gateway-key" }; } },
    backend: createPiAdapter({
      streamers: {
        "openai-responses": () => nativeStream(nativeEvents),
      },
    }),
    audit: {
      async record() {
        auditAttempts += 1;
        throw new Error("audit unavailable");
      },
    },
  });
  const client = {
    infer(request, options = {}) {
      return services.provider.infer(request, {
        signal: options.signal ?? new AbortController().signal,
      });
    },
  };

  const events = [];
  for await (const event of streamProvider(
    client,
    model.id,
    {
      api: "cyclo-pi",
      provider: "work",
      id: "model-a",
      maxTokens: 4_096,
    },
    { messages: [{ role: "user", content: "hello", timestamp: 0 }] },
  )) events.push(event);

  assert.equal(auditAttempts, 1);
  assert.deepEqual(
    events.slice(0, -1).map(({ type }) => type),
    nativeEvents.slice(0, -1).map(({ type }) => type),
  );
  assert.equal(events.some(({ type }) => type === "done"), false);
  assert.equal(events.at(-1).type, "error");
  assert.match(events.at(-1).error.errorMessage, /usage audit unavailable/u);
});

test("waits and replays after gateway 429 exhaustion crosses real ConnectRPC", async () => {
  const now = Date.parse("2031-02-03T04:05:06Z");
  const model = portableModel("work/model-a");
  const selected = {
    api: "cyclo-pi",
    provider: "work",
    id: "model-a",
    maxTokens: 4_096,
  };
  const requests = [];
  const waits = [];
  const audit = [];
  let nativeCalls = 0;
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.assign(Object.create(null), {
        [model.id]: {
          publicModel: model,
          rawModel: {
            id: "model-a",
            provider: "openai",
            api: "openai-responses",
          },
        },
      }),
    },
    credentials: { async resolve() { return { apiKey: "gateway-key" }; } },
    backend: createPiAdapter({
      now: () => now,
      streamers: {
        "openai-responses": (_model, _context, options) => {
          nativeCalls += 1;
          return nativeCalls === 1
            ? nativeUsageLimit(options, { "retry-after-ms": "1500" })
            : nativeStream(successfulEvents(selected));
        },
      },
    }),
    audit: { async record(entry) { audit.push(entry); } },
  });
  const provider = {
    listModels: services.provider.listModels,
    async *infer(request, context) {
      requests.push({ model: request.model, payload: request.payload });
      yield* services.provider.infer(request, context);
    },
  };
  const server = createServer(connectNodeAdapter({
    connect: true,
    grpc: false,
    grpcWeb: false,
    routes(router) { router.service(Provider, provider); },
  }));

  try {
    const port = await listen(server);
    const events = [];
    for await (const event of streamProvider(
      providerClient(`dns:///127.0.0.1:${port}`),
      model.id,
      selected,
      { messages: [{ role: "user", content: "retry through gateway", timestamp: 0 }] },
      {},
      {
        now: () => now,
        async sleep(milliseconds) { waits.push(milliseconds); },
      },
    )) events.push(event);

    assert.equal(nativeCalls, 2);
    assert.deepEqual(waits, [1_500]);
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1], requests[0]);
    assert.equal(requests[0].model, model.id);
    assert.deepEqual(audit.map(({ outcome }) => outcome), [
      `rpc_${Code.ResourceExhausted}`,
      "ok",
    ]);
    assert.equal(events.some(({ type }) => type === "error"), false);
    assert.deepEqual(events, successfulEvents(selected));
  } finally {
    await close(server);
  }
});

test("propagates cancellation through ConnectRPC", async () => {
  await withProvider(async ({ target, state }) => {
    const registration = await oneRegistration(target);
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
  await withProvider(async ({ target }) => {
    const registration = await oneRegistration(target);
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

test("does not turn Pi's provider timeout into an Infer RPC deadline", async () => {
  const controller = new AbortController();
  let callOptions;
  const selected = { provider: "work", id: "model-a", maxTokens: 4_096 };
  const client = {
    async *infer(_request, options) {
      callOptions = options;
      for (const event of successfulEvents(selected)) {
        yield { payload: JSON.stringify(event) };
      }
    },
  };

  const events = [];
  for await (const event of streamProvider(
    client,
    "work/model-a",
    selected,
    { messages: [{ role: "user", content: "long inference", timestamp: 0 }] },
    { signal: controller.signal, timeoutMs: 1 },
  )) events.push(event);

  assert.equal(callOptions.signal, controller.signal);
  assert.equal("timeoutMs" in callOptions, false);
  assert.deepEqual(events, successfulEvents(selected));
});

test("waits outside exhausted RPCs and retries the identical request until it succeeds", async () => {
  const selected = { provider: "work", id: "model-a", maxTokens: 4_096 };
  const requests = [];
  const waits = [];
  let attempt = 0;
  const client = {
    async *infer(request) {
      requests.push(request);
      attempt += 1;
      if (attempt === 1) throw createResourceExhaustedError(new Date(10_500));
      if (attempt === 2) throw createResourceExhaustedError(new Date(11_500));
      for (const event of successfulEvents(selected)) {
        yield { payload: JSON.stringify(event) };
      }
    },
  };

  const events = [];
  for await (const event of streamProvider(
    client,
    "work/model-a",
    selected,
    { messages: [{ role: "user", content: "retry", timestamp: 0 }] },
    {},
    {
      now: () => 10_000,
      async sleep(milliseconds) { waits.push(milliseconds); },
    },
  )) events.push(event);

  assert.deepEqual(waits, [1_000, 1_500]);
  assert.equal(requests.length, 3);
  assert.equal(requests[1], requests[0]);
  assert.equal(requests[2], requests[0]);
  assert.equal(requests[0].model, "work/model-a");
  assert.deepEqual(events, successfulEvents(selected));
});

test("cancellation interrupts an exhaustion wait without starting another RPC", async () => {
  const controller = new AbortController();
  const waiting = deferred();
  let calls = 0;
  const client = {
    async *infer() {
      calls += 1;
      throw createResourceExhaustedError(new Date(20_000));
    },
  };
  const stream = streamProvider(
    client,
    "work/model-a",
    { provider: "work", id: "model-a", maxTokens: 4_096 },
    { messages: [{ role: "user", content: "cancel-wait", timestamp: 0 }] },
    { signal: controller.signal },
    {
      now: () => 10_000,
      sleep(_milliseconds, signal) {
        waiting.resolve();
        return new Promise((resolve, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      },
    },
  );
  const terminal = stream[Symbol.asyncIterator]().next();

  await waiting.promise;
  controller.abort();

  const result = await withTimeout(terminal);
  assert.equal(result.value.type, "error");
  assert.equal(result.value.reason, "aborted");
  assert.equal(calls, 1);
});

test("does not retry exhaustion after the Provider emitted a response", async () => {
  const waits = [];
  let calls = 0;
  const client = {
    async *infer() {
      calls += 1;
      yield { payload: JSON.stringify(startEvent("model-a")) };
      throw createResourceExhaustedError(new Date(20_000));
    },
  };
  const events = [];

  for await (const event of streamProvider(
    client,
    "work/model-a",
    { provider: "work", id: "model-a", maxTokens: 4_096 },
    { messages: [{ role: "user", content: "partial", timestamp: 0 }] },
    {},
    {
      now: () => 10_000,
      async sleep(milliseconds) { waits.push(milliseconds); },
    },
  )) events.push(event);

  assert.equal(calls, 1);
  assert.deepEqual(waits, []);
  assert.deepEqual(events.map(({ type }) => type), ["start", "error"]);
});

test("only retries typed pre-stream resource exhaustion with a valid retry time", async () => {
  const malformed = new ConnectError("quota", Code.ResourceExhausted);
  malformed.details.push({
    type: ResourceExhaustionSchema.typeName,
    value: Uint8Array.of(0xff),
  });
  const cases = [
    ["missing detail", new ConnectError("quota", Code.ResourceExhausted), "error"],
    ["malformed detail", malformed, "error"],
    ["deadline", new ConnectError("deadline", Code.DeadlineExceeded), "error"],
    ["unavailable", new ConnectError("offline", Code.Unavailable), "error"],
    ["canceled", new ConnectError("canceled", Code.Canceled), "aborted"],
  ];

  for (const [name, failure, reason] of cases) {
    let calls = 0;
    let sleeps = 0;
    const client = {
      async *infer() {
        calls += 1;
        throw failure;
      },
    };
    const events = [];
    for await (const event of streamProvider(
      client,
      "work/model-a",
      { provider: "work", id: "model-a", maxTokens: 4_096 },
      { messages: [{ role: "user", content: name, timestamp: 0 }] },
      {},
      { async sleep() { sleeps += 1; } },
    )) events.push(event);

    assert.equal(calls, 1, name);
    assert.equal(sleeps, 0, name);
    assert.deepEqual(events.map(({ type }) => type), ["error"], name);
    assert.equal(events[0].reason, reason, name);
  }
});

test("isolates incompatible catalogue entries without hiding valid models", () => {
  const warnings = [];
  const groups = groupModels([
    { ...portableModel("work/wrong-format"), inferenceFormat: "other" },
    { ...portableModel("work/missing-limit"), maxOutputTokens: undefined },
    portableModel("gateway/reserved"),
    portableModel("work/usable"),
    portableModel("work/usable"),
  ], {
    onInvalid(message) { warnings.push(message); },
  });

  assert.deepEqual(
    groups.get("work").map(({ publicId }) => publicId),
    ["work/usable"],
  );
  assert.equal(warnings.length, 4);
  assert.match(warnings[0], /unsupported inference format/u);
  assert.match(warnings[1], /no usable output limit/u);
  assert.match(warnings[2], /PROVIDER\/MODEL/u);
  assert.match(warnings[3], /duplicate/u);
});

async function withProvider(run) {
  const state = { requests: [], canceled: deferred() };
  const server = createServer(connectNodeAdapter({
    connect: true,
    grpc: false,
    grpcWeb: false,
    routes(router) { router.service(Provider, implementation(state)); },
  }));
  try {
    const port = await listen(server);
    await run({ target: `dns:///127.0.0.1:${port}`, state });
  } finally {
    await close(server);
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

async function* nativeStream(events) {
  for (const event of events) yield event;
}

async function* nativeUsageLimit(options, headers) {
  await options.onResponse({ status: 429, headers });
  yield {
    type: "error",
    reason: "error",
    error: {
      errorMessage: "provider account exhausted",
      usage: zeroUsage(),
    },
  };
}

async function oneRegistration(target) {
  const registrations = new Map();
  await registerCycloProviders({
    registerProvider(name, configuration) { registrations.set(name, configuration); },
  }, { target });
  return registrations.get("work");
}

function selectedModel(registration, id) {
  return { ...registration.models.find((model) => model.id === id), provider: "work" };
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
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
