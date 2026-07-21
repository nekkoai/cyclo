import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { zstdDecompressSync } from "node:zlib";
import test from "node:test";

import { Modality, ToolChoiceMode } from "@cyclo/provider/contract";
import { validateInferStream } from "@cyclo/provider/protocol";

import { createPiAdapter } from "../src/pi-adapter.mjs";

test("the real pi OpenAI adapter preserves auth, schema, choice, and terminal output", async (t) => {
  let request;
  const upstream = createServer(async (incoming, response) => {
    const chunks = [];
    for await (const chunk of incoming) chunks.push(chunk);
    request = {
      method: incoming.method,
      url: incoming.url,
      authorization: incoming.headers.authorization,
      body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
    };

    response.writeHead(200, { "content-type": "text/event-stream" });
    for (const event of nativeEvents()) response.write(`data: ${JSON.stringify(event)}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const { port } = upstream.address();

  const route = {
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
      name: "GPT test",
      provider: "openai",
      api: "openai-responses",
      baseUrl: `http://127.0.0.1:${port}/v1`,
      reasoning: false,
      input: ["text"],
      contextWindow: 4096,
      maxTokens: 1024,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    },
  };
  const schema = {
    type: "object",
    properties: { key: { type: "string", enum: ["x", "y"] } },
    required: ["key"],
    additionalProperties: false,
  };
  const prepared = {
    context: {
      messages: [{ role: "user", content: "hello", timestamp: 0 }],
      tools: [{ name: "lookup", description: "Lookup", parameters: schema }],
    },
    generation: {
      maxTokens: 32,
      toolChoice: { mode: ToolChoiceMode.SPECIFIC, toolName: "lookup" },
    },
  };

  const output = [];
  for await (const value of validateInferStream(
    createPiAdapter().infer(
      route,
      prepared,
      { apiKey: "test-only-native-key", sensitiveValues: ["test-only-native-key"] },
      new AbortController().signal,
    ),
    { model: route.publicModel.id },
  )) output.push(value);

  assert.equal(request.method, "POST");
  assert.equal(request.url, "/v1/responses");
  assert.equal(request.authorization, "Bearer test-only-native-key");
  assert.deepEqual(request.body.tools[0].parameters, schema);
  assert.deepEqual(request.body.tool_choice, { type: "function", name: "lookup" });
  assert.deepEqual(output.map(({ event }) => event.case), [
    "started",
    "itemStarted",
    "itemDelta",
    "itemFinished",
    "finished",
  ]);
  assert.equal(output[2].event.value.delta.value, "native ok");
});

test("the real pi Codex adapter sends the requested output-token limit", async (t) => {
  let request;
  const upstream = createServer(async (incoming, response) => {
    const chunks = [];
    for await (const chunk of incoming) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const decoded = incoming.headers["content-encoding"] === "zstd"
      ? zstdDecompressSync(raw)
      : raw;
    request = {
      url: incoming.url,
      account: incoming.headers["chatgpt-account-id"],
      body: JSON.parse(decoded.toString("utf8")),
    };
    response.writeHead(200, { "content-type": "text/event-stream" });
    for (const event of nativeEvents()) response.write(`data: ${JSON.stringify(event)}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const { port } = upstream.address();
  const payload = Buffer.from(JSON.stringify({
    "https://api.openai.com/auth": { chatgpt_account_id: "account-test" },
  })).toString("base64url");
  const credential = `e30.${payload}.signature`;
  const route = {
    publicModel: {
      id: "codex/gpt-test",
      capabilities: {
        inputModalities: [Modality.TEXT],
        outputModalities: [Modality.TEXT],
        functionTools: true,
      },
    },
    rawModel: {
      id: "gpt-test",
      name: "GPT test",
      provider: "openai-codex",
      api: "openai-codex-responses",
      baseUrl: `http://127.0.0.1:${port}/backend-api`,
      reasoning: false,
      input: ["text"],
      contextWindow: 4096,
      maxTokens: 1024,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    },
  };
  const prepared = {
    context: { messages: [{ role: "user", content: "hello", timestamp: 0 }] },
    generation: {
      maxTokens: 16,
      toolChoice: { mode: ToolChoiceMode.AUTO, toolName: "" },
    },
  };

  const output = [];
  for await (const value of validateInferStream(
    createPiAdapter().infer(
      route,
      prepared,
      { apiKey: credential, sensitiveValues: [credential] },
      new AbortController().signal,
    ),
    { model: route.publicModel.id },
  )) output.push(value);

  assert.equal(request.url, "/backend-api/codex/responses");
  assert.equal(request.account, "account-test");
  assert.equal(request.body.max_output_tokens, 16);
  assert.equal(output.at(-1).event.case, "finished");
});

function nativeEvents() {
  const response = {
    id: "resp_test",
    object: "response",
    created_at: 0,
    model: "gpt-test",
    status: "completed",
    output: [],
    usage: {
      input_tokens: 3,
      input_tokens_details: { cached_tokens: 0 },
      output_tokens: 2,
      output_tokens_details: { reasoning_tokens: 0 },
      total_tokens: 5,
    },
  };
  const item = {
    id: "msg_test",
    type: "message",
    role: "assistant",
    status: "completed",
    content: [{ type: "output_text", text: "native ok", annotations: [] }],
  };
  return [
    { type: "response.created", response },
    { type: "response.output_item.added", output_index: 0, item: { ...item, content: [] } },
    { type: "response.output_text.delta", output_index: 0, item_id: item.id, delta: "native ok" },
    { type: "response.output_item.done", output_index: 0, item },
    { type: "response.completed", response: { ...response, output: [item] } },
  ];
}
