import assert from "node:assert/strict";
import test from "node:test";

import { toJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import { FinishReason, Modality, ToolChoiceMode } from "@cyclo/provider/contract";
import { validateInferStream } from "@cyclo/provider/protocol";

import { createPiAdapter } from "../src/pi-adapter.mjs";

test("maps a native stream, redacts credentials, and reports portable usage", async () => {
  let invocation;
  const adapter = createPiAdapter({
    streamers: {
      "openai-responses": (model, context, options) => {
        invocation = { model, context, options };
        return successfulNativeStream();
      },
    },
  });
  const responses = [];
  for await (const response of validateInferStream(
    adapter.infer(route(), prepared(), credential(), new AbortController().signal),
    { model: "work/gpt-test" },
  )) {
    responses.push(response);
  }

  assert.equal(invocation.options.apiKey, "secret-token");
  assert.equal(invocation.options.maxRetries, 0);
  assert.deepEqual(responses.map(({ event }) => event.case), [
    "started",
    "itemStarted",
    "itemDelta",
    "itemDelta",
    "itemFinished",
    "itemStarted",
    "itemFinished",
    "finished",
  ]);
  assert.equal(
    responses.filter(({ event }) => event.case === "itemDelta")
      .map(({ event }) => event.value.delta.value)
      .join(""),
    "before [REDACTED] after",
  );
  const tool = responses.find(({ event }) => event.case === "itemFinished" && event.value.toolArguments);
  assert.deepEqual(toJson(StructSchema, tool.event.value.toolArguments), {
    value: "[REDACTED]",
  });
  const finished = responses.at(-1).event.value;
  assert.equal(finished.reason, FinishReason.TOOL_CALLS);
  assert.equal(finished.usage.inputTokens, 10n);
  assert.equal(finished.usage.cachedInputTokens, 3n);
  assert.equal(finished.usage.outputTokens, 5n);
  assert.equal(finished.usage.totalTokens, 15n);
  assert.doesNotMatch(JSON.stringify(responses, bigintJson), /secret-token/u);
});

test("turns clean native truncation into DATA_LOSS", async () => {
  const adapter = createPiAdapter({
    streamers: { "openai-responses": () => truncatedNativeStream() },
  });
  const iterator = adapter
    .infer(route(), prepared(), credential(), new AbortController().signal)
    [Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.event.case, "started");
  await assert.rejects(
    iterator.next(),
    (error) => error instanceof ConnectError && error.code === Code.DataLoss,
  );
});

test("preserves native start order for interleaved text and tool calls", async () => {
  const adapter = createPiAdapter({
    streamers: { "openai-responses": () => interleavedNativeStream() },
  });
  const responses = [];
  for await (const response of validateInferStream(
    adapter.infer(route(), prepared(), credential(), new AbortController().signal),
    { model: "work/gpt-test" },
  )) {
    responses.push(response);
  }
  const starts = responses.filter(({ event }) => event.case === "itemStarted");
  assert.deepEqual(starts.map(({ event }) => [event.value.index, event.value.item.case]), [
    [0, "text"],
    [1, "toolCall"],
  ]);
  assert.deepEqual(
    responses.filter(({ event }) => event.case === "itemFinished").map(({ event }) => event.value.index),
    [1, 0],
  );
});

test("maps an aborted handler signal to CANCELED without native detail", async () => {
  const controller = new AbortController();
  controller.abort(new Error("private cancellation detail"));
  const adapter = createPiAdapter({
    streamers: { "openai-responses": () => failedNativeStream() },
  });
  const iterator = adapter.infer(route(), prepared(), credential(), controller.signal)[Symbol.asyncIterator]();
  await assert.rejects(
    iterator.next(),
    (error) => error instanceof ConnectError
      && error.code === Code.Canceled
      && !error.rawMessage.includes("private"),
  );
});

test("aborts native dispatch when local stream validation rejects output", async () => {
  let nativeSignal;
  const adapter = createPiAdapter({
    streamers: {
      "openai-responses": (_model, _context, options) => {
        nativeSignal = options.signal;
        return malformedNativeStream();
      },
    },
  });
  const iterator = adapter
    .infer(route(), prepared(), credential(), new AbortController().signal)
    [Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.event.case, "started");
  await assert.rejects(
    iterator.next(),
    (error) => error instanceof ConnectError && error.code === Code.DataLoss,
  );
  assert.equal(nativeSignal.aborted, true);
});

test("specific Anthropic choices use the OAuth-canonicalized native tool name", async () => {
  let payload;
  const adapter = createPiAdapter({
    streamers: {
      "anthropic-messages": (_model, _context, options) => {
        payload = options.onPayload({
          tools: [{ name: "Read" }],
          tool_choice: { type: "tool", name: "read" },
        });
        return nativeToolStream("Read");
      },
    },
  });
  const request = prepared();
  request.context.tools[0].name = "read";
  request.context.tools[0].parameters = {
    type: "object",
    properties: { path: { type: "string" } },
    required: ["path"],
    additionalProperties: false,
  };
  request.generation.toolChoice = {
    mode: ToolChoiceMode.SPECIFIC,
    toolName: "read",
  };
  const responses = [];
  for await (const response of validateInferStream(adapter.infer(
    route("anthropic-messages", "anthropic"),
    request,
    credential(),
    new AbortController().signal,
  ), { model: "work/gpt-test" })) responses.push(response);
  assert.deepEqual(payload.tool_choice, { type: "tool", name: "Read" });
  assert.deepEqual(payload.tools[0].input_schema, request.context.tools[0].parameters);
  assert.equal(
    responses.find(({ event }) => event.case === "itemStarted").event.value.item.value.name,
    "read",
  );
});

async function* successfulNativeStream() {
  const partial = { content: [] };
  yield { type: "start", partial };
  yield { type: "thinking_start", contentIndex: 0, partial };
  yield { type: "thinking_delta", contentIndex: 0, delta: "hidden", partial };
  yield { type: "thinking_end", contentIndex: 0, content: "hidden", partial };
  yield { type: "text_start", contentIndex: 1, partial };
  yield { type: "text_delta", contentIndex: 1, delta: "before sec", partial };
  yield { type: "text_delta", contentIndex: 1, delta: "ret-token after", partial };
  yield { type: "text_end", contentIndex: 1, content: "before secret-token after", partial };
  partial.content[2] = {
    type: "toolCall",
    id: "call-1|fc-private",
    name: "lookup",
    arguments: {},
  };
  yield { type: "toolcall_start", contentIndex: 2, partial };
  yield {
    type: "toolcall_end",
    contentIndex: 2,
    toolCall: {
      type: "toolCall",
      id: "call-1|fc-private",
      name: "lookup",
      arguments: { value: "secret-token" },
    },
    partial,
  };
  yield {
    type: "done",
    reason: "toolUse",
    message: {
      usage: {
        input: 4,
        output: 5,
        cacheRead: 3,
        cacheWrite: 3,
        reasoning: 2,
      },
    },
  };
}

async function* truncatedNativeStream() {
  yield { type: "start", partial: { content: [] } };
}

async function* failedNativeStream() {
  yield { type: "error", reason: "aborted", error: new Error("native secret") };
}

async function* malformedNativeStream() {
  yield { type: "start", partial: { content: [] } };
  yield { type: "unknown-native-event" };
}

async function* interleavedNativeStream() {
  const partial = { content: [] };
  yield { type: "start", partial };
  partial.content[0] = { type: "text", text: "" };
  yield { type: "text_start", contentIndex: 0, partial };
  partial.content[1] = { type: "toolCall", id: "call-1|fc-1", name: "lookup", arguments: {} };
  yield { type: "toolcall_start", contentIndex: 1, partial };
  yield { type: "text_delta", contentIndex: 0, delta: "ok", partial };
  yield {
    type: "toolcall_end",
    contentIndex: 1,
    toolCall: { type: "toolCall", id: "call-1|fc-1", name: "lookup", arguments: { key: "x" } },
    partial,
  };
  yield { type: "text_end", contentIndex: 0, content: "ok", partial };
  yield {
    type: "done",
    reason: "toolUse",
    message: { usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 } },
  };
}

async function* nativeToolStream(name) {
  const partial = { content: [{ type: "toolCall", id: "toolu_1", name, arguments: {} }] };
  yield { type: "start", partial };
  yield { type: "toolcall_start", contentIndex: 0, partial };
  yield {
    type: "toolcall_end",
    contentIndex: 0,
    toolCall: partial.content[0],
    partial,
  };
  yield {
    type: "done",
    reason: "toolUse",
    message: { usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 } },
  };
}

function route(api = "openai-responses", provider = "openai") {
  return {
    publicModel: {
      id: "work/gpt-test",
      capabilities: {
        inputModalities: [Modality.TEXT],
        outputModalities: [Modality.TEXT],
        functionTools: true,
      },
    },
    rawModel: {
      id: "gpt-test",
      provider,
      api,
      baseUrl: "https://example.invalid/v1",
    },
  };
}

function prepared() {
  return {
    context: {
      messages: [{ role: "user", content: "hello", timestamp: 0 }],
      tools: [{ name: "lookup", description: "", parameters: { type: "object" } }],
    },
    generation: {
      maxTokens: 32,
      toolChoice: { mode: ToolChoiceMode.AUTO, toolName: "" },
    },
  };
}

function credential() {
  return { apiKey: "secret-token", sensitiveValues: ["secret-token"] };
}

function bigintJson(_key, value) {
  return typeof value === "bigint" ? value.toString() : value;
}
