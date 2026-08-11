import assert from "node:assert/strict";
import test from "node:test";

import OpenAI from "openai";

import { createResourceExhaustedError } from "@cyclo/provider/errors";
import { encodePayload } from "@cyclo/provider/protocol";

import { createOpenAIHTTPServer } from "../src/http.mjs";
import {
  assistant,
  deferred,
  providerClient,
  providerModel,
  textEvents,
} from "./helpers.mjs";

test("the official OpenAI SDK can list, retrieve, and invoke Provider models", async () => {
  const observed = [];
  await withServer({
    client: providerClient({
      onInfer(request) { observed.push(request); },
      events: textEvents("sdk response"),
    }),
    apiKey: "local-secret",
  }, async ({ baseURL }) => {
    const openai = new OpenAI({
      apiKey: "local-secret",
      baseURL: `${baseURL}/v1`,
      maxRetries: 0,
    });

    const models = await openai.models.list();
    assert.deepEqual(models.data.map((model) => model.id), ["work/test-model"]);
    assert.equal((await openai.models.retrieve("work/test-model")).owned_by, "work");

    const response = await openai.responses.create({
      model: "work/test-model",
      input: "hello",
      store: false,
    });
    assert.equal(response.status, "completed");
    assert.equal(response.output_text, "sdk response");
    assert.equal(observed.length, 1);
    assert.equal(observed[0].model, "work/test-model");
  });
});

test("the official OpenAI SDK consumes the complete Responses SSE stream", async () => {
  await withServer({ client: providerClient({ events: textEvents("streamed") }) }, async ({ baseURL }) => {
    const openai = new OpenAI({
      apiKey: "unused",
      baseURL: `${baseURL}/v1`,
      maxRetries: 0,
    });
    const stream = await openai.responses.create({
      model: "work/test-model",
      input: "hello",
      stream: true,
      store: false,
    });
    const events = [];
    for await (const event of stream) events.push(event);

    assert.deepEqual(events.slice(0, 2).map((event) => event.type), [
      "response.created",
      "response.in_progress",
    ]);
    assert.equal(events.find((event) => event.type === "response.output_text.delta").delta, "streamed");
    assert.equal(events.at(-1).type, "response.completed");
    assert.equal(events.at(-1).response.output_text, "streamed");
  });
});

test("enforces optional bearer authentication and returns OpenAI-shaped HTTP errors", async () => {
  await withServer({
    client: providerClient(),
    apiKey: "right-secret",
    maxRequestBytes: 32,
  }, async ({ baseURL }) => {
    const unauthorized = await fetch(`${baseURL}/v1/models`, {
      headers: { authorization: "Bearer wrong-secret" },
    });
    assert.equal(unauthorized.status, 401);
    assert.match(unauthorized.headers.get("www-authenticate"), /Bearer/u);
    assert.deepEqual((await unauthorized.json()).error, {
      message: "Incorrect API key provided",
      type: "authentication_error",
      param: null,
      code: "invalid_api_key",
    });

    const malformed = await fetch(`${baseURL}/v1/responses`, {
      method: "POST",
      headers: {
        authorization: "Bearer right-secret",
        "content-type": "text/plain",
      },
      body: "not json",
    });
    assert.equal(malformed.status, 415);
    assert.equal((await malformed.json()).error.code, "unsupported_media_type");

    const oversized = await fetch(`${baseURL}/v1/responses`, {
      method: "POST",
      headers: {
        authorization: "Bearer right-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({ model: "work/test-model", input: "too large" }),
    });
    assert.equal(oversized.status, 413);
    assert.equal((await oversized.json()).error.code, "request_too_large");

    const missing = await fetch(`${baseURL}/v1/unknown`, {
      headers: { authorization: "Bearer right-secret" },
    });
    assert.equal(missing.status, 404);
    assert.equal((await missing.json()).error.code, "not_found");
  });
});

test("maps typed pre-admission Provider exhaustion to retryable HTTP 429", async () => {
  const now = 1_700_000_000_000;
  const exhausted = createResourceExhaustedError(new Date(now + 2_100));
  await withServer({
    client: providerClient({ inferError: exhausted }),
    now: () => now,
  }, async ({ baseURL }) => {
    const response = await fetch(`${baseURL}/v1/responses`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "work/test-model", input: "hello" }),
    });
    assert.equal(response.status, 429);
    assert.equal(response.headers.get("retry-after"), "3");
    assert.deepEqual((await response.json()).error, {
      message: "Cyclo Provider capacity is exhausted; retry later",
      type: "rate_limit_error",
      param: null,
      code: "rate_limit_exceeded",
    });
  });
});

test("an HTTP disconnect cancels the active Provider stream", async () => {
  const canceled = deferred();
  const client = {
    async listModels() {
      return { models: [providerModel()] };
    },
    async *infer(_request, options) {
      try {
        yield { payload: encodePayload({ type: "start", partial: assistant([]) }) };
        await new Promise((resolve, reject) => {
          if (options.signal.aborted) {
            reject(options.signal.reason);
            return;
          }
          options.signal.addEventListener(
            "abort",
            () => reject(options.signal.reason),
            { once: true },
          );
        });
      } finally {
        canceled.resolve();
      }
    },
  };

  await withServer({ client }, async ({ baseURL }) => {
    const controller = new AbortController();
    const response = await fetch(`${baseURL}/v1/responses`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "work/test-model", input: "wait", stream: true }),
      signal: controller.signal,
    });
    assert.equal(response.status, 200);
    const reader = response.body.getReader();
    assert.equal((await reader.read()).done, false);
    controller.abort();
    await assert.rejects(reader.read(), /abort/u);
    await withTimeout(canceled.promise);
  });
});

async function withServer(options, run) {
  const server = createOpenAIHTTPServer(options);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  try {
    await run({
      server,
      baseURL: `http://127.0.0.1:${address.port}`,
    });
  } finally {
    server.closeAllConnections();
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    }).catch((error) => {
      if (error.code !== "ERR_SERVER_NOT_RUNNING") throw error;
    });
  }
}

function withTimeout(promise, timeoutMs = 1_000) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error("timed out")), timeoutMs).unref();
    }),
  ]);
}
