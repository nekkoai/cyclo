import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { lstat, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { create, fromJson, toBinary } from "@bufbuild/protobuf";
import { AnySchema, StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { resolveBindings } from "@cyclo/component/bindings";
import { Component, HealthStatus } from "@cyclo/component/contract";
import { parseDeclaration } from "@cyclo/component/declaration";
import {
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { createUnixTransport } from "@cyclo/component/transport";
import {
  FinishReason,
  InferRequestSchema,
  InferResponseSchema,
  ListModelsResponseSchema,
  MessageRole,
  Modality,
  Provider,
  ToolChoiceMode,
} from "@cyclo/provider/contract";

import { createPassthroughServices } from "../src/services.mjs";
import { runPassthrough } from "../src/main.mjs";
import { createPassthroughServer } from "../src/server.mjs";
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

test("forwards complete protobuf semantics incrementally using only its bound UDS", async () => {
  const gate = deferred();
  await withPassthrough({ gate }, async ({ component, provider, observed }) => {
    const hostileHeaders = {
      authorization: "Bearer caller-secret",
      cookie: "session=caller-secret",
      "x-api-key": "caller-secret",
      "x-forwarded-for": "198.51.100.1",
    };
    const catalogue = await provider.listModels({}, { headers: hostileHeaders });
    assertBytesEqual(
      ListModelsResponseSchema,
      catalogue,
      completeCatalogue(),
    );

    const request = completeRequest("success");
    const iterator = provider.infer(request, { headers: hostileHeaders })[Symbol.asyncIterator]();
    const first = await iterator.next();
    assert.equal(first.value.event.case, "started");

    let secondSettled = false;
    const secondPending = iterator.next().then((result) => {
      secondSettled = true;
      return result;
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(secondSettled, false, "pass-through buffered beyond Started");
    gate.resolve();

    const responses = [first.value, (await secondPending).value];
    for (;;) {
      const next = await iterator.next();
      if (next.done) break;
      responses.push(next.value);
    }

    assertBytesEqual(InferRequestSchema, observed.requests[0], request);
    const expected = completeResponses(request.model);
    assert.equal(responses.length, expected.length);
    responses.forEach((response, index) => {
      assertBytesEqual(InferResponseSchema, response, expected[index]);
    });
    assert.equal(observed.listCalls, 1);
    assert.equal(observed.inferCalls, 1);
    assertUpstreamHeaders(observed.headers[0]);
    assertUpstreamHeaders(observed.headers[1]);

    assert.equal((await component.health({}, { headers: hostileHeaders })).status, HealthStatus.READY);
    assertUpstreamHeaders(observed.headers[2]);
  });
});

test("reports dependency health generically and recovers", async () => {
  await withPassthrough({}, async ({ component, observed }) => {
    observed.failure = new ConnectError(
      "sensitive upstream failure at /private/upstream.sock",
      Code.Unauthenticated,
    );
    const unavailable = await component.health({});
    assert.deepEqual(unavailable, {
      $typeName: "cyclo.component.v1.HealthResponse",
      status: HealthStatus.NOT_READY,
      message: "upstream provider unavailable",
    });
    assert.doesNotMatch(unavailable.message, /private|sensitive|socket/u);

    observed.failure = undefined;
    assert.equal((await component.health({})).status, HealthStatus.READY);

    observed.hang = true;
    const started = Date.now();
    assert.equal((await component.health({})).status, HealthStatus.NOT_READY);
    assert.ok(Date.now() - started < 500, "health probe exceeded its bound");
  });
});

test("does not retry and preserves failure semantics without exposing a false Finished", async () => {
  await withPassthrough({}, async ({ provider, observed }) => {
    for (const [command, expectedCode, emitted] of [
      ["fail-before", Code.Unavailable, []],
      ["fail-after", Code.Unavailable, ["started"]],
      ["truncate", Code.DataLoss, ["started"]],
      ["fail-after-finished", Code.Unavailable, ["started"]],
      ["event-after-finished", Code.DataLoss, ["started"]],
    ]) {
      const before = observed.inferCalls;
      const events = [];
      await assert.rejects(
        async () => {
          for await (const response of provider.infer(completeRequest(command))) {
            events.push(response.event.case);
          }
        },
        (error) => error instanceof ConnectError && error.code === expectedCode,
      );
      assert.deepEqual(events, emitted, command);
      assert.equal(observed.inferCalls, before + 1, `${command} was retried`);
    }
  });
});

test("propagates cancellation to the upstream stream", async () => {
  const canceled = deferred();
  await withPassthrough({ canceled }, async ({ provider, observed }) => {
    const controller = new AbortController();
    const iterator = provider
      .infer(completeRequest("wait"), { signal: controller.signal })
      [Symbol.asyncIterator]();
    assert.equal((await iterator.next()).value.event.case, "started");
    controller.abort();
    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.Canceled,
    );
    await withTimeout(canceled.promise);
    assert.equal(observed.inferCalls, 1);
  });
});

test("SIGTERM aborts active inference and removes the component socket", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-passthrough-signal-"));
  const upstreamSocket = join(directory, "upstream.sock");
  const outputSocket = join(directory, "output.sock");
  const canceled = deferred();
  const observed = { headers: [], inferCalls: 0, listCalls: 0, requests: [] };
  const upstream = makeUpstreamServer(observed, { canceled });
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
    const iterator = provider.infer(completeRequest("wait"))[Symbol.asyncIterator]();
    assert.equal((await iterator.next()).value.event.case, "started");

    signalSource.emit("SIGTERM");
    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.Unavailable,
    );
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

  const observed = {
    failure: undefined,
    headers: [],
    inferCalls: 0,
    listCalls: 0,
    requests: [],
  };
  const upstream = makeUpstreamServer(observed, options);
  let passthrough;
  try {
    await listenComponentServer(upstream, { socketPath: upstreamSocket });
    const binding = await createUpstreamBinding({
      env: {
        CYCLO_REQUIRE_UPSTREAM_SOCKET: upstreamSocket,
      },
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
    options.gate?.resolve();
    if (passthrough) await closeComponentServer(passthrough).catch(() => {});
    if (upstream) await closeComponentServer(upstream).catch(() => {});
    await rm(directory, { recursive: true, force: true });
  }
}

function makeUpstreamServer(observed, { gate, canceled } = {}) {
  const bindings = resolveBindings(
    parseDeclaration(`
      component test-upstream
      provide cyclo.component.v1.Component
      provide cyclo.provider.v1.Provider
    `),
    [Component, Provider],
  );
  return createComponentServer({
    bindings,
    implementations: new Map([
      [Component.typeName, { health: () => ({ status: HealthStatus.READY }) }],
      [Provider.typeName, {
        listModels(_request, context) {
          observeCall(observed, context);
          observed.listCalls += 1;
          if (observed.failure) throw observed.failure;
          if (observed.hang) {
            return aborted(context.signal).then(() => {
              throw context.signal.reason;
            });
          }
          return completeCatalogue();
        },
        async *infer(request, context) {
          observeCall(observed, context);
          observed.inferCalls += 1;
          observed.requests.push(request);
          const command = request.instructions;
          if (command === "fail-before") {
            throw new ConnectError("upstream failed before output", Code.Unavailable);
          }
          yield completeResponses(request.model)[0];
          if (command === "fail-after") {
            throw new ConnectError("upstream failed after output", Code.Unavailable);
          }
          if (command === "truncate") return;
          if (command === "wait") {
            try {
              await aborted(context.signal);
              throw context.signal.reason;
            } finally {
              canceled?.resolve();
            }
          }
          if (command === "fail-after-finished" || command === "event-after-finished") {
            yield response("finished", { reason: FinishReason.STOP });
            if (command === "fail-after-finished") {
              throw new ConnectError("upstream failed after Finished", Code.Unavailable);
            }
            yield create(InferResponseSchema, {
              event: { case: "itemStarted", value: { index: 0, item: { case: "text", value: {} } } },
            });
            return;
          }
          if (command === "success") await gate.promise;
          const remaining = completeResponses(request.model).slice(1);
          for (const response of remaining) yield response;
        },
      }],
    ]),
  });
}

function observeCall(observed, context) {
  observed.headers.push(new Headers(context.requestHeader));
}

function assertUpstreamHeaders(headers) {
  assert.equal(headers.get("authorization"), null);
  assert.equal(headers.get("cookie"), null);
  assert.equal(headers.get("x-api-key"), null);
  assert.equal(headers.get("x-forwarded-for"), null);
}

function completeCatalogue() {
  return create(ListModelsResponseSchema, {
    models: [{
      id: "opaque/model",
      displayName: "Opaque model",
      capabilities: {
        inputModalities: [Modality.TEXT, Modality.IMAGE],
        outputModalities: [Modality.TEXT, Modality.IMAGE],
        functionTools: true,
        parallelToolCalls: true,
        reasoningSummaries: true,
        temperature: true,
        topP: true,
        stopSequences: true,
        extensionTypes: ["example.v1.State"],
      },
      contextWindowTokens: 8192n,
      maxOutputTokens: 1024n,
      extensions: [any("example.v1.Catalogue", [0, 255, 1])],
    }],
  });
}

function completeRequest(command) {
  return create(InferRequestSchema, {
    model: "opaque/model",
    instructions: command,
    input: [
      {
        item: {
          case: "message",
          value: {
            role: MessageRole.USER,
            content: [
              { content: { case: "text", value: "hello" } },
              { content: { case: "media", value: { mediaType: "image/png", data: bytes(0, 1, 255) } } },
            ],
          },
        },
        extensions: [any("example.v1.Item", [2, 3])],
      },
      {
        item: {
          case: "toolCall",
          value: { id: "old-call", name: "lookup", arguments: structure({ key: "value" }) },
        },
      },
      {
        item: {
          case: "toolResult",
          value: {
            callId: "old-call",
            content: [{ content: { case: "text", value: "result" } }],
            isError: true,
          },
        },
      },
      { item: { case: "reasoningSummary", value: { text: "prior summary" } } },
      { item: { case: "extension", value: any("example.v1.State", [4, 5, 6]) } },
    ],
    tools: [{
      name: "lookup",
      description: "Look something up",
      inputSchema: structure({ type: "object", required: ["key"] }),
    }],
    generation: {
      maxOutputTokens: 77n,
      temperature: 0,
      topP: 0.5,
      stopSequences: ["END"],
      toolChoice: { mode: ToolChoiceMode.SPECIFIC, toolName: "lookup" },
    },
    extensions: [any("example.v1.Request", [9, 8, 7])],
  });
}

function completeResponses(model) {
  const extension = any("example.v1.State", [0, 127, 255]);
  return [
    response("started", { responseId: "response-1", model, extensions: [extension] }),
    response("itemStarted", { index: 0, item: { case: "text", value: {} } }),
    response("itemStarted", {
      index: 1,
      item: { case: "reasoningSummary", value: {} },
    }),
    response("itemDelta", { index: 1, delta: { case: "text", value: "summary" } }),
    response("itemDelta", { index: 0, delta: { case: "text", value: "answer" } }),
    response("itemFinished", { index: 1, extensions: [extension] }),
    response("itemFinished", { index: 0 }),
    response("itemStarted", {
      index: 2,
      item: { case: "toolCall", value: { id: "call-1", name: "lookup" } },
    }),
    response("itemDelta", {
      index: 2,
      delta: { case: "toolArgumentsJson", value: "{\"key\":\"value\"}" },
    }),
    response("itemFinished", {
      index: 2,
      toolArguments: structure({ key: "value" }),
    }),
    response("itemStarted", {
      index: 3,
      item: { case: "media", value: { mediaType: "image/png" } },
    }),
    response("itemDelta", { index: 3, delta: { case: "media", value: bytes(0, 128, 255) } }),
    response("itemFinished", { index: 3 }),
    response("itemStarted", { index: 4, item: { case: "extension", value: extension } }),
    response("itemFinished", { index: 4, extensions: [extension] }),
    response("finished", {
      reason: FinishReason.STOP_SEQUENCE,
      stopSequence: "END",
      usage: {
        inputTokens: 10n,
        outputTokens: 4n,
        totalTokens: 14n,
        cachedInputTokens: 3n,
        reasoningTokens: 1n,
      },
      extensions: [extension],
    }),
  ];
}

function response(caseName, value) {
  return create(InferResponseSchema, { event: { case: caseName, value } });
}

function any(typeName, values) {
  return create(AnySchema, {
    typeUrl: `type.googleapis.com/${typeName}`,
    value: bytes(...values),
  });
}

function structure(value) {
  return fromJson(StructSchema, value);
}

function bytes(...values) {
  return Uint8Array.from(values);
}

function assertBytesEqual(schema, actual, expected) {
  assert.deepEqual(toBinary(schema, actual), toBinary(schema, expected));
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function aborted(signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
}

function withTimeout(promise, timeoutMs = 1_000) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error("timed out waiting for cancellation")),
        timeoutMs,
      );
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
