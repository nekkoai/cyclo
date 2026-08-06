import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import test from "node:test";

import { Code, ConnectError } from "@connectrpc/connect";
import { resourceExhaustedRetryAt } from "@cyclo/provider/errors";

import { createPiAdapter } from "../src/pi-adapter.mjs";

test("the real Pi adapter sends an arbitrary tool schema unchanged", async (t) => {
  let request;
  const upstream = createServer(async (incoming, response) => {
    const chunks = [];
    for await (const chunk of incoming) chunks.push(chunk);
    request = {
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
  const schema = {
    type: "object",
    properties: {
      refreshRunners: {
        anyOf: [{ type: "boolean" }, { type: "string", enum: ["always", "never"] }],
      },
    },
    "x-unknown-keyword": { nested: true },
  };
  const context = {
    messages: [{ role: "user", content: "hello", timestamp: 0 }],
    tools: [{ name: "lens_diagnostics", description: "Lens", parameters: schema }],
  };
  const output = [];
  for await (const response of createPiAdapter().infer(
    route(`http://127.0.0.1:${port}/v1`),
    JSON.stringify({ context, options: { maxTokens: 32 } }),
    { apiKey: "test-only-key" },
    new AbortController().signal,
  )) output.push(JSON.parse(response.payload));

  assert.equal(request.authorization, "Bearer test-only-key");
  assert.deepEqual(request.body.tools[0].parameters, schema);
  assert.deepEqual(output.map(({ type }) => type), [
    "start",
    "text_start",
    "text_delta",
    "text_end",
    "done",
  ]);
  assert.equal(output[2].delta, "native ok");
});

test("the real Codex adapter returns body-only exhaustion without sleeping", async (t) => {
  const now = Date.now();
  let requests = 0;
  const upstream = createServer(async (incoming, response) => {
    for await (const _chunk of incoming) {
      // Drain the request before replying.
    }
    requests += 1;
    if (requests === 1) {
      response.writeHead(429, { "content-type": "application/json" });
      response.end(JSON.stringify({
        error: {
          code: "usage_limit_reached",
          message: "Monthly usage limit reached",
          plan_type: "plus",
          resets_at: Math.floor(now / 1000) + 5 * 60,
        },
      }));
      return;
    }
    response.writeHead(200, { "content-type": "text/event-stream" });
    for (const event of nativeEvents()) response.write(`data: ${JSON.stringify(event)}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const { port } = upstream.address();
  const adapter = createPiAdapter({
    now: () => now,
  });

  await assert.rejects(
    collect(adapter.infer(
      route(`http://127.0.0.1:${port}`, "openai-codex-responses"),
      JSON.stringify({
        context: { messages: [{ role: "user", content: "hello", timestamp: 0 }] },
        options: { maxTokens: 32 },
      }),
      { apiKey: codexToken() },
      new AbortController().signal,
    )),
    (error) => error instanceof ConnectError
      && error.code === Code.ResourceExhausted
      && resourceExhaustedRetryAt(error)?.getTime() === now + 5 * 60_000,
  );

  assert.equal(requests, 1);
});

test("the real Anthropic and OpenAI adapters expose SDK 429s as exhaustion", async (t) => {
  const now = Date.parse("2031-02-03T04:05:06Z");
  const cases = [
    ["anthropic-messages", {
      type: "error",
      error: { type: "rate_limit_error", message: "private Anthropic quota detail" },
    }],
    ["openai-responses", {
      error: {
        type: "rate_limit_error",
        code: "rate_limit_exceeded",
        message: "private OpenAI quota detail",
      },
    }],
  ];

  for (const [api, errorBody] of cases) {
    await t.test(api, async (subtest) => {
      let requests = 0;
      const upstream = createServer(async (incoming, response) => {
        for await (const _chunk of incoming) {
          // Drain the request before replying.
        }
        requests += 1;
        response.writeHead(429, {
          "content-type": "application/json",
          "retry-after": "7200",
        });
        response.end(JSON.stringify(errorBody));
      });
      upstream.listen(0, "127.0.0.1");
      await once(upstream, "listening");
      subtest.after(() => new Promise((resolve) => upstream.close(resolve)));
      const { port } = upstream.address();

      await assert.rejects(
        collect(createPiAdapter({ now: () => now }).infer(
          route(`http://127.0.0.1:${port}${api === "openai-responses" ? "/v1" : ""}`, api),
          JSON.stringify({
            context: { messages: [{ role: "user", content: "hello", timestamp: 0 }] },
            options: { maxTokens: 32 },
          }),
          { apiKey: "test-only-key" },
          new AbortController().signal,
        )),
        (error) => error instanceof ConnectError
          && error.code === Code.ResourceExhausted
          && resourceExhaustedRetryAt(error)?.getTime() === now + 60_000
          && !error.message.includes("private"),
      );
      assert.equal(requests, 1);
    });
  }
});

function route(baseUrl, api = "openai-responses") {
  const provider = api === "anthropic-messages"
    ? "anthropic"
    : api === "openai-codex-responses"
      ? "openai-codex"
      : "openai";
  return {
    publicModel: { id: "work/gpt-test" },
    rawModel: {
      id: "gpt-test",
      name: "GPT test",
      provider,
      api,
      baseUrl,
      reasoning: false,
      input: ["text"],
      contextWindow: 4096,
      maxTokens: 1024,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    },
  };
}

function codexToken() {
  const header = Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify({
    "https://api.openai.com/auth": { chatgpt_account_id: "test-account" },
  })).toString("base64url");
  return `${header}.${payload}.signature`;
}

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

async function collect(values) {
  const result = [];
  for await (const value of values) result.push(value);
  return result;
}
