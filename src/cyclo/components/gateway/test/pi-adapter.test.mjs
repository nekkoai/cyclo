import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { Code, ConnectError } from "@connectrpc/connect";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { createPiAdapter } from "../src/pi-adapter.mjs";

test("pins the native Pi implementation to the advertised inference format", async () => {
  const manifest = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(
    PI_INFERENCE_FORMAT,
    `pi-ai@${manifest.dependencies["@earendil-works/pi-ai"]}`,
  );
});

test("decodes only the Pi call frame and preserves every native event", async () => {
  let invocation;
  const events = [
    { type: "start", partial: { future: { nested: true } } },
    {
      type: "future_pi_event",
      schema: { anyOf: [{ type: "boolean" }, { enum: ["always"] }] },
      unknown: [null, 3, "\u2603"],
    },
    { type: "done", reason: "stop", message: { usage: usage() } },
  ];
  const adapter = createPiAdapter({
    streamers: {
      "openai-responses": (model, context, options) => {
        invocation = { model, context, options };
        return stream(events);
      },
    },
  });
  const frame = {
    context: {
      messages: [{ role: "future-role", content: "opaque" }],
      tools: [{ name: "odd name", parameters: { anyOf: [] } }],
      futureContextField: { untouched: true },
    },
    options: {
      reasoning: "high",
      futureOption: { untouched: true },
      apiKey: "hostile-key",
      headers: { authorization: "hostile" },
      env: { SECRET: "hostile" },
      transport: "websocket",
      timeoutMs: 999_999,
      websocketConnectTimeoutMs: 999_999,
      maxRetries: 99,
      maxRetryDelayMs: 999_999,
    },
  };

  const responses = [];
  for await (const response of adapter.infer(
    route(),
    JSON.stringify(frame),
    { apiKey: "gateway-key" },
    new AbortController().signal,
  )) responses.push(response);

  assert.deepEqual(invocation.context, frame.context);
  assert.equal(invocation.options.reasoning, "high");
  assert.deepEqual(invocation.options.futureOption, { untouched: true });
  assert.equal(invocation.options.apiKey, "gateway-key");
  assert.equal(invocation.options.maxRetries, 0);
  assert.equal(invocation.options.headers, undefined);
  assert.equal(invocation.options.env, undefined);
  assert.equal(invocation.options.transport, undefined);
  assert.equal(invocation.options.timeoutMs, undefined);
  assert.equal(invocation.options.websocketConnectTimeoutMs, undefined);
  assert.equal(invocation.options.maxRetryDelayMs, undefined);
  assert.deepEqual(responses.map(({ payload }) => JSON.parse(payload)), events);
  assert.deepEqual(responses.at(-1).usage, {
    inputTokens: 14,
    outputTokens: 5,
    cachedInputTokens: 3,
    reasoningTokens: 2,
  });
});

test("preserves Pi event content that contains no gateway credential", async () => {
  const event = {
    type: "done",
    reason: "future-reason",
    message: {
      content: [{
        type: "future-content",
        text: "provider output remains byte-for-byte JSON data",
        schema: { __proto__: null, anyOf: [true, false] },
      }],
      usage: usage(),
    },
  };
  const adapter = createPiAdapter({
    streamers: { "openai-responses": () => stream([event]) },
  });
  const [response] = await collect(adapter.infer(
    route(),
    JSON.stringify({ context: {}, options: {} }),
    { apiKey: "credential" },
    new AbortController().signal,
  ));
  assert.equal(response.payload, JSON.stringify(event));
});

test("fails closed when a native event reflects gateway authentication material", async () => {
  const apiKey = "comma,key";
  const headerSecret = "quote\"slash\\line\nsnowman\u2603";
  const event = {
    type: "error",
    [`header-${headerSecret}`]: "credential in a property name",
    error: {
      errorMessage: `upstream rejected Bearer ${apiKey} twice: ${apiKey}`,
    },
  };
  const original = structuredClone(event);
  const adapter = createPiAdapter({
    streamers: { "openai-responses": () => stream([event]) },
  });

  await assert.rejects(
    collect(adapter.infer(
      route(),
      JSON.stringify({ context: {}, options: {} }),
      { apiKey, secretValues: [apiKey, headerSecret] },
      new AbortController().signal,
    )),
    (error) => error instanceof ConnectError
      && error.code === Code.DataLoss
      && !error.rawMessage.includes(apiKey)
      && !error.rawMessage.includes(headerSecret),
  );
  assert.deepEqual(event, original);
});

test("protects an authentication-header value independently of the API key", async () => {
  const apiKey = "unreflected-api-key";
  const headerSecret = "private-auth-header-value";
  const adapter = createPiAdapter({
    streamers: {
      "openai-responses": () => stream([{
        type: "error",
        error: { errorMessage: `upstream reflected ${headerSecret}` },
      }]),
    },
  });

  await assert.rejects(
    collect(adapter.infer(
      route(),
      JSON.stringify({ context: {}, options: {} }),
      { apiKey, secretValues: [apiKey, headerSecret] },
      new AbortController().signal,
    )),
    (error) => error instanceof ConnectError
      && error.code === Code.DataLoss
      && !error.rawMessage.includes(headerSecret),
  );
});

test("sanitizes Connect errors thrown by a native iterator", async () => {
  const secret = "private-native-error-secret";
  const adapter = createPiAdapter({
    streamers: {
      "openai-responses": () => failingStream(new ConnectError(
        `native error reflected ${secret}`,
        Code.Internal,
        { "x-upstream-error": secret },
      )),
    },
  });

  await assert.rejects(
    collect(adapter.infer(
      route(),
      JSON.stringify({ context: {}, options: {} }),
      { apiKey: secret },
      new AbortController().signal,
    )),
    (error) => error instanceof ConnectError
      && error.code === Code.Unavailable
      && error.rawMessage === "upstream inference failed"
      && !Array.from(error.metadata.entries()).flat().join("\n").includes(secret),
  );
});

test("rejects malformed framing but never validates context contents", async () => {
  const adapter = createPiAdapter({ streamers: { "openai-responses": () => stream([]) } });
  for (const payload of ["not-json", "null", "{}", '{"context":{},"options":[]}']) {
    const iterator = adapter.infer(
      route(),
      payload,
      { apiKey: "credential" },
      new AbortController().signal,
    )[Symbol.asyncIterator]();
    await assert.rejects(
      iterator.next(),
      (error) => error instanceof ConnectError && error.code === Code.InvalidArgument,
    );
  }
});

test("maps cancellation and upstream failures to transport errors", async () => {
  const controller = new AbortController();
  controller.abort(new Error("private detail"));
  const adapter = createPiAdapter({
    streamers: { "openai-responses": () => { throw new Error("private upstream"); } },
  });
  const iterator = adapter.infer(
    route(),
    JSON.stringify({ context: {}, options: {} }),
    { apiKey: "credential" },
    controller.signal,
  )[Symbol.asyncIterator]();
  await assert.rejects(
    iterator.next(),
    (error) => error instanceof ConnectError
      && error.code === Code.Canceled
      && !error.rawMessage.includes("private"),
  );
});

function route() {
  return {
    publicModel: { id: "work/model" },
    rawModel: {
      id: "model",
      provider: "openai",
      api: "openai-responses",
      baseUrl: "https://example.invalid/v1",
    },
  };
}

function usage() {
  return {
    input: 7,
    output: 5,
    cacheRead: 3,
    cacheWrite: 4,
    reasoning: 2,
    totalTokens: 19,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

async function* stream(events) {
  for (const event of events) yield event;
}

async function* failingStream(error) {
  throw error;
}

async function collect(values) {
  const result = [];
  for await (const value of values) result.push(value);
  return result;
}
