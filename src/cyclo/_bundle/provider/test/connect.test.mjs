import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { fromJson, toJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { connectNodeAdapter, createConnectTransport } from "@connectrpc/connect-node";
import { Component, HealthStatus } from "@cyclo/component/contract";
import { registerProvides, resolveBindings } from "@cyclo/component/bindings";
import { parseDeclaration } from "@cyclo/component/declaration";
import {
  FinishReason,
  MessageRole,
  Modality,
  Provider,
} from "@cyclo/provider/contract";
import { validateInferStream } from "@cyclo/provider/protocol";

const passDeclarationUrl = new URL("fixtures/passthrough.conf", import.meta.url);

test("a generated Provider streams incrementally through a pass-through", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client, releaseSecond, protocols }) => {
    const catalogue = await client.listModels({});
    assert.deepEqual(
      catalogue.models.map(({ id }) => id),
      ["portable-model", "text-model"],
    );

    const stream = client.infer(textRequest("portable-model", "hello"));
    const iterator = stream[Symbol.asyncIterator]();
    const first = await iterator.next();

    assert.equal(first.done, false);
    assert.equal(first.value.event.case, "started");
    assert.equal(first.value.event.value.model, "portable-model");

    let secondSettled = false;
    const secondPending = iterator.next().then((result) => {
      secondSettled = true;
      return result;
    });
    await turn();
    assert.equal(secondSettled, false, "the first event was buffered with the second");

    releaseSecond();
    const second = await secondPending;
    const third = await iterator.next();
    const fourth = await iterator.next();
    const fifth = await iterator.next();
    const eof = await iterator.next();

    assert.equal(second.value.event.case, "itemStarted");
    assert.equal(second.value.event.value.index, 0);
    assert.equal(second.value.event.value.item.case, "text");
    assert.deepEqual(eventSummary(third.value), [0, "text", "hello"]);
    assert.equal(fourth.value.event.case, "itemFinished");
    assert.equal(fourth.value.event.value.index, 0);
    assert.equal(fifth.value.event.case, "finished");
    assert.equal(fifth.value.event.value.reason, FinishReason.STOP);
    assert.equal(fifth.value.event.value.usage.inputTokens, 3n);
    assert.equal(fifth.value.event.value.usage.outputTokens, 1n);
    assert.equal(eof.done, true);
    assert.deepEqual(protocols, ["passthrough:connect", "upstream:connect"]);
  });
});

test("cancellation crosses the pass-through and cleans up both streams", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client, cancellations }) => {
    const controller = new AbortController();
    const iterator = client
      .infer(textRequest("portable-model", "cancel"), { signal: controller.signal })
      [Symbol.asyncIterator]();

    const first = await iterator.next();
    assert.equal(first.value.event.case, "started");
    controller.abort();

    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.Canceled,
    );
    await Promise.all([
      withTimeout(cancellations.upstream),
      withTimeout(cancellations.passthrough),
    ]);
  });
});

test("Connect errors remain the only failure channel before and after output", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client }) => {
    const before = client
      .infer(textRequest("portable-model", "fail-before"))
      [Symbol.asyncIterator]();
    await assert.rejects(
      before.next(),
      (error) => error instanceof ConnectError && error.code === Code.Unavailable,
    );

    const after = client
      .infer(textRequest("portable-model", "fail-after"))
      [Symbol.asyncIterator]();
    const started = await after.next();
    assert.equal(started.value.event.case, "started");
    await assert.rejects(
      after.next(),
      (error) => error instanceof ConnectError && error.code === Code.Unavailable,
    );
  });
});

test("indexed items preserve interleaved parallel tool calls", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client }) => {
    const responses = [];
    for await (const response of client.infer(interleavedRequest())) {
      responses.push(response);
    }

    assert.deepEqual(
      responses.map(({ event }) => event.case),
      [
        "started",
        "itemStarted",
        "itemStarted",
        "itemDelta",
        "itemDelta",
        "itemDelta",
        "itemFinished",
        "itemFinished",
        "finished",
      ],
    );
    assert.deepEqual(
      responses.slice(1, 3).map(({ event }) => [
        event.value.index,
        event.value.item.value.id,
      ]),
      [[0, "call-a"], [1, "call-b"]],
    );
    assert.deepEqual(
      responses.slice(3, 6).map(({ event }) => [
        event.value.index,
        event.value.delta.value,
      ]),
      [[0, '{"x":'], [1, '{"y":2}'], [0, "1}"]],
    );
    assert.deepEqual(
      responses.slice(6, 8).map(({ event }) => [
        event.value.index,
        toJson(StructSchema, event.value.toolArguments),
      ]),
      [[1, { y: 2 }], [0, { x: 1 }]],
    );
  });
});

test("an unsupported capability is rejected before Started", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client }) => {
    const iterator = client
      .infer({ ...textRequest("text-model", "hello"), tools: [{ name: "shell" }] })
      [Symbol.asyncIterator]();

    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.InvalidArgument,
    );
  });
});

test("an unknown model is rejected before Started", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client }) => {
    const iterator = client
      .infer(textRequest("missing-model", "hello"))
      [Symbol.asyncIterator]();

    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.NotFound,
    );
  });
});

test("a cleanly truncated upstream stream becomes DATA_LOSS", { timeout: 5_000 }, async () => {
  await withProviderPair(async ({ client }) => {
    const iterator = client
      .infer(textRequest("portable-model", "truncate"))
      [Symbol.asyncIterator]();

    const first = await iterator.next();
    assert.equal(first.value.event.case, "started");
    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.DataLoss,
    );
  });
});

async function withProviderPair(run) {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-provider-"));
  const upstreamSocket = join(directory, "upstream.sock");
  const passthroughSocket = join(directory, "passthrough.sock");
  const protocols = [];
  const secondGate = deferred();
  const upstreamCanceled = deferred();
  const passthroughCanceled = deferred();

  const upstreamBindings = resolveBindings(
    parseDeclaration(`
      component upstream
      provide cyclo.component.v1.Component
      provide cyclo.provider.v1.Provider
    `),
    [Component, Provider],
  );
  const upstream = componentServer(
    upstreamBindings,
    new Map([
      [Component.typeName, healthImplementation()],
      [
        Provider.typeName,
        {
          listModels() {
            return {
              models: [
                {
                  id: "portable-model",
                  displayName: "Portable model",
                  capabilities: {
                    inputModalities: [Modality.TEXT],
                    outputModalities: [Modality.TEXT],
                    functionTools: true,
                    parallelToolCalls: true,
                  },
                },
                {
                  id: "text-model",
                  displayName: "Text model",
                  capabilities: {
                    inputModalities: [Modality.TEXT],
                    outputModalities: [Modality.TEXT],
                  },
                },
              ],
            };
          },
          async *infer(request, context) {
            protocols.push(`upstream:${context.protocolName}`);
            const command = requestText(request);
            if (request.model !== "portable-model" && request.model !== "text-model") {
              throw new ConnectError("unknown model", Code.NotFound);
            }
            if (request.model === "text-model" && request.tools.length > 0) {
              throw new ConnectError("function tools are unsupported", Code.InvalidArgument);
            }
            if (command === "fail-before") {
              throw new ConnectError("upstream unavailable", Code.Unavailable);
            }
            try {
              yield started(request.model);
              if (command === "fail-after") {
                throw new ConnectError("upstream unavailable", Code.Unavailable);
              }
              if (command === "cancel") {
                await aborted(context.signal);
                return;
              }
              if (command === "interleave") {
                yield* interleavedToolEvents();
                return;
              }
              if (command === "truncate") return;
              await secondGate.promise;
              yield {
                event: {
                  case: "itemStarted",
                  value: { index: 0, item: { case: "text", value: {} } },
                },
              };
              yield {
                event: {
                  case: "itemDelta",
                  value: {
                    index: 0,
                    delta: { case: "text", value: requestText(request) },
                  },
                },
              };
              yield {
                event: { case: "itemFinished", value: { index: 0 } },
              };
              yield {
                event: {
                  case: "finished",
                  value: {
                    reason: FinishReason.STOP,
                    usage: { inputTokens: 3n, outputTokens: 1n, totalTokens: 4n },
                  },
                },
              };
            } finally {
              if (command === "cancel") upstreamCanceled.resolve();
            }
          },
        },
      ],
    ]),
  );

  const passDeclaration = parseDeclaration(
    await readFile(passDeclarationUrl, "utf8"),
    { source: passDeclarationUrl.pathname },
  );
  const passBindings = resolveBindings(passDeclaration, [Component, Provider]);

  try {
    await listen(upstream, upstreamSocket);
    const upstreamClient = providerClient(
      passBindings.requires.get("upstream"),
      upstreamSocket,
    );
    const passthrough = componentServer(
      passBindings,
      new Map([
        [Component.typeName, healthImplementation()],
        [
          Provider.typeName,
          {
            listModels(request, context) {
              return upstreamClient.listModels(request, { signal: context.signal });
            },
            async *infer(request, context) {
              protocols.push(`passthrough:${context.protocolName}`);
              try {
                yield* validateInferStream(
                  upstreamClient.infer(request, { signal: context.signal }),
                  { model: request.model },
                );
              } finally {
                if (requestText(request) === "cancel") passthroughCanceled.resolve();
              }
            },
          },
        ],
      ]),
    );

    try {
      await listen(passthrough, passthroughSocket);
      await run({
        client: providerClient(Provider, passthroughSocket),
        releaseSecond: secondGate.resolve,
        protocols,
        cancellations: {
          upstream: upstreamCanceled.promise,
          passthrough: passthroughCanceled.promise,
        },
      });
    } finally {
      secondGate.resolve();
      await close(passthrough);
    }
  } finally {
    secondGate.resolve();
    await close(upstream);
    await rm(directory, { recursive: true, force: true });
  }
}

function componentServer(bindings, implementations) {
  return createServer(
    connectNodeAdapter({
      connect: true,
      grpc: false,
      grpcWeb: false,
      routes(router) {
        registerProvides(router, bindings, implementations);
      },
    }),
  );
}

function providerClient(descriptor, socketPath) {
  return createClient(
    descriptor,
    createConnectTransport({
      baseUrl: "http://localhost",
      httpVersion: "1.1",
      nodeOptions: { socketPath },
    }),
  );
}

function healthImplementation() {
  return {
    health() {
      return { status: HealthStatus.READY, message: "ready" };
    },
  };
}

function textRequest(model, text) {
  return {
    model,
    instructions: "Reply briefly.",
    input: [
      {
        item: {
          case: "message",
          value: {
            role: MessageRole.USER,
            content: [{ content: { case: "text", value: text } }],
          },
        },
      },
    ],
  };
}

function interleavedRequest() {
  return {
    ...textRequest("portable-model", "interleave"),
    tools: [
      { name: "first", inputSchema: fromJson(StructSchema, { type: "object" }) },
      { name: "second", inputSchema: fromJson(StructSchema, { type: "object" }) },
    ],
  };
}

function requestText(request) {
  return request.input[0].item.value.content[0].content.value;
}

function started(model) {
  return {
    event: {
      case: "started",
      value: { responseId: `response-${model}`, model },
    },
  };
}

function eventSummary(response) {
  return [
    response.event.value.index,
    response.event.value.delta.case,
    response.event.value.delta.value,
  ];
}

function* interleavedToolEvents() {
  yield toolStarted(0, "call-a", "first");
  yield toolStarted(1, "call-b", "second");
  yield toolDelta(0, '{"x":');
  yield toolDelta(1, '{"y":2}');
  yield toolDelta(0, "1}");
  yield toolFinished(1, { y: 2 });
  yield toolFinished(0, { x: 1 });
  yield {
    event: { case: "finished", value: { reason: FinishReason.TOOL_CALLS } },
  };
}

function toolStarted(index, id, name) {
  return {
    event: {
      case: "itemStarted",
      value: {
        index,
        item: { case: "toolCall", value: { id, name } },
      },
    },
  };
}

function toolDelta(index, value) {
  return {
    event: {
      case: "itemDelta",
      value: {
        index,
        delta: { case: "toolArgumentsJson", value },
      },
    },
  };
}

function toolFinished(index, argumentsValue) {
  return {
    event: {
      case: "itemFinished",
      value: {
        index,
        toolArguments: fromJson(StructSchema, argumentsValue),
      },
    },
  };
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

function turn() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function withTimeout(promise, milliseconds = 2_000) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("timed out")), milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function listen(server, path) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(path, () => {
      server.off("error", reject);
      resolve();
    });
  });
}

function close(server) {
  if (!server?.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}
