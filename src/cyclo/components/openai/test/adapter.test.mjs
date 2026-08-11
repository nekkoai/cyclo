import assert from "node:assert/strict";
import test from "node:test";

import { Modality } from "@cyclo/provider/contract";
import { decodePayload } from "@cyclo/provider/protocol";

import {
  OpenAIRequestError,
  compatibleModels,
  createOpenAIResponse,
  openAIModels,
  streamOpenAIResponse,
} from "../src/adapter.mjs";
import {
  assistant,
  providerClient,
  providerModel,
  textEvents,
} from "./helpers.mjs";

test("exposes only unique models compatible with the pinned Pi ABI", () => {
  const valid = providerModel();
  const diagnostics = [];
  const models = [
    valid,
    providerModel({ displayName: "duplicate" }),
    providerModel({ id: "invalid id" }),
    providerModel({ id: "work/future", inferenceFormat: "future@1" }),
    providerModel({
      id: "work/audio",
      capabilities: { inputModalities: [Modality.TEXT, Modality.AUDIO] },
    }),
    providerModel({ id: "work/no-limit", maxOutputTokens: 0n }),
  ];

  assert.deepEqual(openAIModels(models, { onInvalid: diagnostics.push.bind(diagnostics) }), [{
    id: valid.id,
    object: "model",
    created: 0,
    owned_by: "work",
  }]);
  assert.equal(compatibleModels(models).size, 1);
  assert.equal(diagnostics.length, 5);
  assert.match(diagnostics.join("\n"), /duplicate model/u);
  assert.match(diagnostics.join("\n"), /unsupported inference format/u);
});

test("translates OpenAI input, history, functions, images, and options into one Pi frame", async () => {
  let observed;
  const client = providerClient({
    events: [
      { type: "start", partial: assistant([]) },
      { type: "done", reason: "stop", message: assistant([]) },
    ],
    onInfer(request, options) {
      observed = { request, options };
    },
  });
  const request = {
    model: "work/test-model",
    instructions: "Be exact.",
    input: [
      {
        role: "user",
        content: [
          { type: "input_text", text: "inspect" },
          { type: "input_image", image_url: "data:image/png;base64,aGk=", detail: "auto" },
        ],
      },
      {
        type: "message",
        id: "msg_previous",
        role: "assistant",
        phase: "final_answer",
        content: [{
          type: "output_text",
          text: "checking",
          annotations: [],
          logprobs: [],
        }],
      },
      {
        type: "function_call",
        id: "fc_previous",
        call_id: "call_previous",
        name: "lookup",
        arguments: "{\"key\":\"value\"}",
      },
      {
        type: "function_call_output",
        call_id: "call_previous",
        output: "found",
      },
    ],
    tools: [{
      type: "function",
      name: "lookup",
      description: "Lookup a value",
      strict: true,
      parameters: {
        type: "object",
        properties: { key: { type: "string" } },
        required: ["key"],
        additionalProperties: false,
      },
    }],
    max_output_tokens: 512,
    temperature: 0.25,
    top_p: 0.8,
    prompt_cache_key: "session-1",
    prompt_cache_retention: "24h",
    reasoning: { effort: "high", summary: "auto" },
    metadata: { trace: "test" },
  };

  const response = await createOpenAIResponse(client, request, { now: () => 1_700_000_000_000 });
  assert.equal(response.status, "completed");
  assert.equal(observed.request.model, request.model);
  assert.deepEqual(observed.options, {});
  const frame = decodePayload(observed.request.payload);
  assert.equal(frame.context.systemPrompt, "Be exact.");
  assert.equal(frame.context.messages.length, 3);
  assert.deepEqual(frame.context.messages[0].content, [
    { type: "text", text: "inspect" },
    { type: "image", mimeType: "image/png", data: "aGk=" },
  ]);
  assert.deepEqual(frame.context.messages[1].content, [
    {
      type: "text",
      text: "checking",
      textSignature: JSON.stringify({
        v: 1,
        id: "msg_previous",
        phase: "final_answer",
      }),
    },
    {
      type: "toolCall",
      id: "call_previous|fc_previous",
      name: "lookup",
      arguments: { key: "value" },
    },
  ]);
  assert.deepEqual(frame.context.messages[2].content, [{ type: "text", text: "found" }]);
  assert.deepEqual(frame.context.tools[0].constrainedSampling, {
    type: "json_schema",
    strict: "require",
  });
  assert.deepEqual(frame.options, {
    maxTokens: 512,
    temperature: 0.25,
    samplingParams: { top_p: 0.8 },
    metadata: { trace: "test" },
    sessionId: "session-1",
    cacheRetention: "long",
    reasoning: "high",
  });
});

test("converts a Pi text stream and usage into an OpenAI response", async () => {
  let next = 0;
  const response = await createOpenAIResponse(
    providerClient({ events: textEvents("hello world") }),
    { model: "work/test-model", input: "say hello", stream: false },
    {
      now: () => 1_700_000_000_000,
      idFactory: (prefix) => `${prefix}_${++next}`,
    },
  );

  assert.equal(response.id, "resp_1");
  assert.equal(response.status, "completed");
  assert.equal(response.output_text, "hello world");
  assert.deepEqual(response.output, [{
    id: "msg_2",
    type: "message",
    status: "completed",
    role: "assistant",
    content: [{
      type: "output_text",
      text: "hello world",
      annotations: [],
      logprobs: [],
    }],
  }]);
  assert.deepEqual(response.usage, {
    input_tokens: 16,
    input_tokens_details: { cached_tokens: 3, cache_write_tokens: 2 },
    output_tokens: 7,
    output_tokens_details: { reasoning_tokens: 0 },
    total_tokens: 23,
  });
});

test("preserves stable reasoning, text, and function-call identities in stream events", async () => {
  const partial = assistant([
    {
      type: "thinking",
      thinking: "",
      thinkingSignature: JSON.stringify({
        type: "reasoning",
        id: "rs_known",
        encrypted_content: "opaque",
      }),
    },
    {
      type: "text",
      text: "",
      textSignature: JSON.stringify({ v: 1, id: "msg_known", phase: "commentary" }),
    },
    { type: "toolCall", id: "call_known|fc_known", name: "lookup", arguments: {} },
  ]);
  const complete = assistant([
    { ...partial.content[0], thinking: "thought" },
    { ...partial.content[1], text: "answer" },
    { ...partial.content[2], arguments: { q: "x" } },
  ], { stopReason: "toolUse" });
  const events = [
    { type: "start", partial },
    { type: "thinking_start", contentIndex: 0, partial },
    { type: "text_start", contentIndex: 1, partial },
    { type: "toolcall_start", contentIndex: 2, partial },
    { type: "text_delta", contentIndex: 1, delta: "answer", partial: complete },
    { type: "thinking_delta", contentIndex: 0, delta: "thought", partial: complete },
    { type: "toolcall_delta", contentIndex: 2, delta: "{\"q\"", partial: complete },
    { type: "thinking_end", contentIndex: 0, content: "thought", partial: complete },
    { type: "text_end", contentIndex: 1, content: "answer", partial: complete },
    { type: "toolcall_end", contentIndex: 2, toolCall: complete.content[2], partial: complete },
    { type: "done", reason: "toolUse", message: complete },
  ];
  const converted = [];
  for await (const event of streamOpenAIResponse(
    providerClient({ events }),
    {
      model: "work/test-model",
      input: "go",
      include: ["reasoning.encrypted_content"],
      stream: true,
    },
  )) converted.push(event);

  assert.deepEqual(converted.slice(0, 2).map((event) => event.type), [
    "response.created",
    "response.in_progress",
  ]);
  assert.deepEqual(
    converted.map((event) => event.sequence_number),
    [...converted.keys()],
  );
  const added = converted.filter((event) => event.type === "response.output_item.added");
  const done = converted.filter((event) => event.type === "response.output_item.done");
  assert.deepEqual(added.map((event) => event.item.id), ["rs_known", "msg_known", "fc_known"]);
  assert.deepEqual(done.map((event) => event.item.id), ["rs_known", "msg_known", "fc_known"]);
  const terminal = converted.at(-1).response;
  assert.equal(terminal.status, "completed");
  assert.equal(terminal.output[0].encrypted_content, "opaque");
  assert.equal(terminal.output[1].phase, "commentary");
  assert.equal(terminal.output[2].call_id, "call_known");
  assert.equal(terminal.output[2].arguments, "{\"q\":\"x\"}");
});

test("round-trips provider-specific reasoning signatures through encrypted_content", async () => {
  const signature = "provider-private-signature";
  const partial = assistant([{
    type: "thinking",
    thinking: "",
    thinkingSignature: signature,
  }]);
  const complete = assistant([{
    type: "thinking",
    thinking: "summary",
    thinkingSignature: signature,
  }]);
  const response = await createOpenAIResponse(
    providerClient({ events: [
      { type: "start", partial },
      { type: "thinking_start", contentIndex: 0, partial },
      { type: "thinking_delta", contentIndex: 0, delta: "summary", partial: complete },
      { type: "thinking_end", contentIndex: 0, content: "summary", partial: complete },
      { type: "done", reason: "stop", message: complete },
    ] }),
    {
      model: "work/test-model",
      input: "think",
      include: ["reasoning.encrypted_content"],
    },
  );
  const reasoning = response.output[0];
  assert.notEqual(reasoning.encrypted_content, signature);
  assert.match(reasoning.encrypted_content, /^cyclo-pi-signature-v1:/u);

  let observed;
  await createOpenAIResponse(
    providerClient({
      events: [
        { type: "start", partial: assistant([]) },
        { type: "done", reason: "stop", message: assistant([]) },
      ],
      onInfer(request) { observed = decodePayload(request.payload); },
    }),
    { model: "work/test-model", input: [reasoning] },
  );
  assert.equal(
    observed.context.messages[0].content[0].thinkingSignature,
    signature,
  );
});

test("turns a malformed or unterminated admitted Provider stream into response.failed", async () => {
  const partial = assistant([{ type: "text", text: "" }]);
  for (const events of [
    [{ type: "start", partial: assistant([]) }],
    [
      { type: "start", partial: assistant([]) },
      { type: "future_event" },
    ],
    [
      { type: "start", partial },
      { type: "text_start", contentIndex: 0, partial },
      { type: "text_delta", contentIndex: 0, delta: "first", partial },
      { type: "text_end", contentIndex: 0, content: "different", partial },
    ],
    [
      { type: "start", partial },
      { type: "text_start", contentIndex: 0, partial },
      { type: "done", reason: "stop", message: assistant([]) },
    ],
  ]) {
    const response = await createOpenAIResponse(
      providerClient({ events }),
      { model: "work/test-model", input: "go" },
    );
    assert.equal(response.status, "failed");
    assert.equal(response.error.code, "server_error");
  }
});

test("rejects OpenAI features that the stateless Pi frame cannot represent", async () => {
  const cases = [
    [{ model: "work/test-model" }, "input"],
    [{ model: "work/test-model", input: "x", store: true }, "store"],
    [{ model: "work/test-model", input: "x", previous_response_id: "resp_1" }, "previous_response_id"],
    [{ model: "work/test-model", input: "x", top_p: 0 }, "top_p"],
    [{ model: "work/test-model", input: "x", max_output_tokens: 5_000 }, "max_output_tokens"],
    [{
      model: "work/test-model",
      input: [{ role: "user", content: [{ type: "input_image", image_url: "https://x" }] }],
    }, "input[0].content[0].image_url"],
    [{ model: "work/test-model", input: "x", tools: [{ type: "web_search" }] }, "tools[0]"],
    [{ model: "work/test-model", input: "x", text: { format: { type: "json_object" } } }, "text"],
  ];

  for (const [request, param] of cases) {
    await assert.rejects(
      createOpenAIResponse(providerClient(), request),
      (error) => error instanceof OpenAIRequestError && error.param === param,
    );
  }
});
