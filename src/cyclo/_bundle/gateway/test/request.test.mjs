import assert from "node:assert/strict";
import test from "node:test";

import { fromJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import {
  MessageRole,
  Modality,
  ToolChoiceMode,
} from "@cyclo/provider/contract";

import { prepareInference } from "../src/request.mjs";

test("validates and maps portable history before dispatch", () => {
  const prepared = prepareInference({
    model: "work/gpt-test",
    instructions: "Be precise.",
    tools: [{
      name: "lookup",
      description: "Look up a value",
      inputSchema: jsonStruct({
        type: "object",
        properties: { key: { type: "string" } },
        required: ["key"],
      }),
    }],
    input: [
      message(MessageRole.USER, "find x"),
      { item: { case: "toolCall", value: {
        id: "call-1",
        name: "lookup",
        arguments: jsonStruct({ key: "x" }),
      } } },
      { item: { case: "toolResult", value: {
        callId: "call-1",
        content: [textPart("42")],
      } } },
    ],
    generation: {
      maxOutputTokens: 32n,
      toolChoice: { mode: ToolChoiceMode.REQUIRED },
    },
  }, route());

  assert.equal(prepared.context.systemPrompt, "Be precise.");
  assert.deepEqual(prepared.context.messages.map(({ role }) => role), [
    "user",
    "assistant",
    "toolResult",
  ]);
  assert.equal(prepared.context.messages[1].content[0].id, "call-1");
  assert.equal(prepared.context.messages[2].toolName, "lookup");
  assert.equal(prepared.generation.maxTokens, 32);
  assert.equal(prepared.generation.toolChoice.mode, ToolChoiceMode.REQUIRED);
});

test("rejects unsupported controls and malformed history before dispatch", () => {
  assertInvalid(
    { ...baseRequest(), generation: { topP: 0.5 } },
    /top_p is unsupported/u,
  );
  assertInvalid(
    {
      ...baseRequest(),
      input: [{ item: { case: "toolResult", value: { callId: "missing" } } }],
    },
    /does not match/u,
  );
  assertInvalid(
    { ...baseRequest(), extensions: [{ typeUrl: "type.example/opaque" }] },
    /does not accept request extensions/u,
  );
});

test("preserves Pi tool schemas while validating the portable envelope", () => {
  const request = {
    model: "work/gpt-test",
    tools: [{
      name: "lookup_1",
      inputSchema: jsonStruct({
        type: "object",
        properties: {
          kind: { type: "string", enum: ["short", "long"] },
          values: { type: "array", items: { type: "integer" }, maxItems: 4 },
        },
        required: ["kind"],
        additionalProperties: false,
      }),
    }],
    input: [{ item: { case: "message", value: {
      role: MessageRole.USER,
      content: [{ content: { case: "media", value: {
        mediaType: "image/png",
        data: new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
      } } }],
    } } }],
  };
  const prepared = prepareInference(request, route());
  assert.equal(prepared.context.tools[0].parameters.additionalProperties, false);
  assert.equal(prepared.context.messages[0].content[0].mimeType, "image/png");

  assertInvalid({
    ...baseRequest(),
    tools: [{
      name: "bad name",
      inputSchema: jsonStruct({ type: "object" }),
    }],
  }, /tool names/u);
  const lensSchema = {
    type: "object",
    properties: {
      refreshRunners: {
        anyOf: [
          { type: "boolean" },
          { type: "string", enum: ["cached", "cheap", "all", "none"] },
        ],
      },
    },
  };
  const lens = prepareInference({
    ...baseRequest(),
    tools: [{ name: "lens_diagnostics", inputSchema: jsonStruct(lensSchema) }],
  }, route());
  assert.deepEqual(lens.context.tools[0].parameters, lensSchema);
  assertInvalid({
    model: "work/gpt-test",
    input: [{ item: { case: "message", value: {
      role: MessageRole.USER,
      content: [{ content: { case: "media", value: {
        mediaType: "image/svg+xml",
        data: new Uint8Array([1]),
      } } }],
    } } }],
  }, /not a valid inline image/u);
});

test("rejects response-API error results rather than silently weakening them", () => {
  const request = {
    model: "work/gpt-test",
    tools: [{ name: "lookup", inputSchema: jsonStruct({ type: "object" }) }],
    input: [
      message(MessageRole.USER, "look"),
      { item: { case: "toolCall", value: {
        id: "call_1",
        name: "lookup",
        arguments: jsonStruct({}),
      } } },
      { item: { case: "toolResult", value: {
        callId: "call_1",
        content: [textPart("failed")],
        isError: true,
      } } },
    ],
  };
  assertInvalid(request, /error tool results are unsupported/u);
  assert.equal(
    prepareInference(request, route("anthropic-messages")).context.messages.at(-1).isError,
    true,
  );
});

function assertInvalid(request, pattern) {
  assert.throws(
    () => prepareInference(request, route()),
    (error) => error instanceof ConnectError
      && error.code === Code.InvalidArgument
      && pattern.test(error.rawMessage),
  );
}

function baseRequest() {
  return { model: "work/gpt-test", input: [message(MessageRole.USER, "hello")] };
}

function route(api = "openai-responses") {
  return {
    publicModel: {
      id: "work/gpt-test",
      maxOutputTokens: 1024n,
      capabilities: {
        inputModalities: [Modality.TEXT, Modality.IMAGE],
        outputModalities: [Modality.TEXT],
        functionTools: true,
        parallelToolCalls: true,
        temperature: true,
      },
    },
    rawModel: {
      id: "gpt-test",
      provider: "openai",
      api,
      baseUrl: "https://example.invalid/v1",
      maxTokens: 1024,
    },
  };
}

function message(role, text) {
  return { item: { case: "message", value: { role, content: [textPart(text)] } } };
}

function textPart(value) {
  return { content: { case: "text", value } };
}

function jsonStruct(value) {
  return fromJson(StructSchema, value);
}
