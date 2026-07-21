import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { fromJson, toJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { connectNodeAdapter } from "@connectrpc/connect-node";
import {
  FinishReason,
  MessageRole,
  Modality,
  Provider,
} from "@cyclo/provider/contract";

import { groupModels } from "../src/adapter.mjs";
import { registerCycloProviders } from "../src/extension.mjs";

test("discovers models and translates one real UDS inference without credentials", async () => {
  await withProvider(async ({ socketPath, state }) => {
    const registrations = new Map();
    const count = await registerCycloProviders({
      registerProvider(name, configuration) {
        registrations.set(name, configuration);
      },
    }, { socketPath });

    assert.equal(count, 2);
    assert.deepEqual([...registrations], [
      ["other", registrations.get("other")],
      ["work", registrations.get("work")],
    ]);
    assert.deepEqual(
      registrations.get("work").models.map(({ id }) => id),
      ["nested/model-b", "model-a"],
    );
    assert.equal(
      registrations.get("other").streamSimple,
      registrations.get("work").streamSimple,
      "all Pi providers must share the complete route table",
    );

    const registration = registrations.get("work");
    const selected = {
      ...registration.models[0],
      provider: "work",
      api: registration.api,
      baseUrl: registration.baseUrl,
    };
    assert.equal(selected.capabilities, undefined, "simulate Pi's normalized model object");
    const controller = new AbortController();
    const events = [];
    for await (const event of registration.streamSimple(selected, context(), {
      signal: controller.signal,
      maxTokens: 32,
      temperature: 0.25,
      apiKey: "must-not-cross",
      headers: { authorization: "Bearer must-not-cross" },
      env: { PRIVATE_VALUE: "must-not-cross" },
      metadata: { private: "must-not-cross" },
    })) events.push(event);

    assert.equal(state.requests.length, 1);
    const request = state.requests[0];
    assert.equal(request.model, "work/nested/model-b");
    assert.equal(request.instructions, "Be exact.");
    assert.equal(request.generation.maxOutputTokens, 32n);
    assert.equal(request.generation.temperature, 0.25);
    assert.deepEqual(request.input.map(({ item }) => item.case), [
      "message",
      "message",
      "toolCall",
      "toolResult",
    ]);
    assert.equal(request.input[0].item.value.role, MessageRole.USER);
    assert.equal(request.input[0].item.value.content[1].content.case, "media");
    assert.deepEqual(toJson(StructSchema, request.tools[0].inputSchema), {
      type: "object",
      properties: { key: { type: "string" } },
      required: ["key"],
    });
    assert.doesNotMatch(JSON.stringify(state.headers), /must-not-cross|authorization/iu);

    assert.deepEqual(events.map(({ type }) => type), [
      "start",
      "text_start",
      "text_delta",
      "toolcall_start",
      "toolcall_delta",
      "text_end",
      "toolcall_end",
      "done",
    ]);
    assert.equal(events[2].delta, "hello");
    assert.deepEqual(events[6].toolCall.arguments, { key: "value" });
    assert.deepEqual(events.at(-1).message.usage, {
      input: 7,
      output: 5,
      cacheRead: 3,
      cacheWrite: 0,
      reasoning: 2,
      totalTokens: 15,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    });

    const other = registrations.get("other");
    const otherEvents = [];
    for await (const event of other.streamSimple(
      selectedModel(other, "model-c", "other"),
      userContext("second"),
      {},
    )) otherEvents.push(event);
    assert.equal(otherEvents.at(-1).type, "done");
    assert.equal(state.requests.at(-1).model, "other/model-c");
  });
});

test("propagates cancellation through the real UDS stream", async () => {
  await withProvider(async ({ socketPath, state }) => {
    const registration = await oneRegistration(socketPath);
    const controller = new AbortController();
    const iterator = registration.streamSimple(
      selectedModel(registration, "model-a"),
      userContext("cancel"),
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

test("turns a malformed clean stream into a Pi error terminal", async () => {
  await withProvider(async ({ socketPath }) => {
    const registration = await oneRegistration(socketPath);
    const events = [];
    for await (const event of registration.streamSimple(
      selectedModel(registration, "model-a"),
      userContext("truncate"),
      {},
    )) events.push(event);

    assert.deepEqual(events.map(({ type }) => type), ["start", "error"]);
    assert.equal(events.at(-1).reason, "error");
    assert.equal(events.at(-1).error.errorMessage, "Cyclo provider request failed");
  });
});

test("rejects usage Pi cannot faithfully represent", async () => {
  await withProvider(async ({ socketPath }) => {
    const registration = await oneRegistration(socketPath);
    const events = [];
    for await (const event of registration.streamSimple(
      selectedModel(registration, "model-a"),
      userContext("partial-usage"),
      {},
    )) events.push(event);

    assert.deepEqual(events.map(({ type }) => type), ["start", "error"]);
  });
});

test("rejects unsafe or unrepresentable catalogue entries", () => {
  for (const [model, pattern] of [
    [{ ...portableModel("gateway/model") }, /PROVIDER\/MODEL/u],
    [{ ...portableModel("work/model"), contextWindowTokens: undefined }, /context window/u],
    [{ ...portableModel("work/model"), extensions: [{ typeUrl: "example.Extension" }] }, /extensions/u],
    [{
      ...portableModel("work/model"),
      capabilities: {
        ...portableModel("work/model").capabilities,
        outputModalities: [Modality.IMAGE],
      },
    }, /output modalities/u],
  ]) assert.throws(() => groupModels([model]), pattern);

  assert.throws(
    () => groupModels([portableModel("work/model"), portableModel("work/model")]),
    /duplicate/u,
  );
});

test("fails before Infer rather than dropping signed Pi history", async () => {
  await withProvider(async ({ socketPath, state }) => {
    const registration = await oneRegistration(socketPath);
    const events = [];
    for await (const event of registration.streamSimple(
      selectedModel(registration, "model-a"),
      {
        messages: [{
          role: "assistant",
          content: [{ type: "text", text: "opaque", textSignature: "signature" }],
          timestamp: 0,
        }],
      },
      {},
    )) events.push(event);

    assert.equal(state.requests.length, 0);
    assert.deepEqual(events.map(({ type }) => type), ["error"]);
  });
});

async function withProvider(run) {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-pi-provider-"));
  const socketPath = join(directory, "component.sock");
  const state = { requests: [], headers: [], canceled: deferred() };
  const server = createServer(connectNodeAdapter({
    connect: true,
    grpc: false,
    grpcWeb: false,
    routes(router) {
      router.service(Provider, providerImplementation(state));
    },
  }));

  try {
    await listen(server, socketPath);
    await run({ socketPath, state });
  } finally {
    await close(server);
    await rm(directory, { recursive: true, force: true });
  }
}

function providerImplementation(state) {
  return {
    listModels() {
      return {
        models: [
          portableModel("other/model-c"),
          portableModel("work/nested/model-b"),
          portableModel("work/model-a"),
        ],
      };
    },
    async *infer(request, context_) {
      state.requests.push(request);
      state.headers.push(Object.fromEntries(context_.requestHeader));
      const command = request.input[0].item.value.content[0].content.value;
      yield started(request.model);
      if (command === "cancel") {
        try {
          await aborted(context_.signal);
        } finally {
          state.canceled.resolve();
        }
        return;
      }
      if (command === "truncate") return;
      if (command === "partial-usage") {
        yield {
          event: {
            case: "finished",
            value: {
              reason: FinishReason.STOP,
              usage: { outputTokens: 1n },
            },
          },
        };
        return;
      }

      yield itemStarted(0, { case: "text", value: {} });
      yield itemDelta(0, { case: "text", value: "hello" });
      yield itemStarted(1, {
        case: "toolCall",
        value: { id: "call-2", name: "lookup" },
      });
      yield itemDelta(1, { case: "toolArgumentsJson", value: '{"key":' });
      yield { event: { case: "itemFinished", value: { index: 0 } } };
      yield {
        event: {
          case: "itemFinished",
          value: { index: 1, toolArguments: jsonStruct({ key: "value" }) },
        },
      };
      yield {
        event: {
          case: "finished",
          value: {
            reason: FinishReason.TOOL_CALLS,
            usage: {
              inputTokens: 10n,
              outputTokens: 5n,
              totalTokens: 15n,
              cachedInputTokens: 3n,
              reasoningTokens: 2n,
            },
          },
        },
      };
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
      reasoningSummaries: false,
      temperature: true,
      extensionTypes: [],
    },
    contextWindowTokens: 128_000n,
    maxOutputTokens: 4_096n,
  };
}

function context() {
  return {
    systemPrompt: "Be exact.",
    tools: [{
      name: "lookup",
      description: "Look up a key",
      parameters: {
        type: "object",
        properties: { key: { type: "string" } },
        required: ["key"],
      },
    }],
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: "question" },
          { type: "image", mimeType: "image/png", data: "iVBORw0KGgo=" },
        ],
        timestamp: 0,
      },
      {
        role: "assistant",
        content: [
          { type: "text", text: "checking" },
          { type: "toolCall", id: "call-1", name: "lookup", arguments: { key: "old" } },
        ],
        api: "cyclo-provider-v1",
        provider: "work",
        model: "nested/model-b",
        timestamp: 0,
      },
      {
        role: "toolResult",
        toolCallId: "call-1",
        toolName: "lookup",
        content: [{ type: "text", text: "old-value" }],
        isError: false,
        timestamp: 0,
      },
    ],
  };
}

function userContext(text) {
  return { messages: [{ role: "user", content: text, timestamp: 0 }] };
}

async function oneRegistration(socketPath) {
  const registrations = new Map();
  await registerCycloProviders({
    registerProvider(name, configuration) {
      registrations.set(name, configuration);
    },
  }, { socketPath });
  return registrations.get("work");
}

function selectedModel(registration, id, provider = "work") {
  return {
    ...registration.models.find((model) => model.id === id),
    provider,
    api: registration.api,
    baseUrl: registration.baseUrl,
  };
}

function started(model) {
  return { event: { case: "started", value: { responseId: "response-1", model } } };
}

function itemStarted(index, item) {
  return { event: { case: "itemStarted", value: { index, item } } };
}

function itemDelta(index, delta) {
  return { event: { case: "itemDelta", value: { index, delta } } };
}

function jsonStruct(value) {
  return fromJson(StructSchema, value);
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
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

function aborted(signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
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
