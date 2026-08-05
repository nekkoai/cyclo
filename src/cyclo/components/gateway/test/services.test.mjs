import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { Code, ConnectError, createClient } from "@connectrpc/connect";
import { HealthStatus } from "@cyclo/component/contract";
import { closeComponentServer, listenComponentServer } from "@cyclo/component/server";
import { createDockerTransport } from "@cyclo/component/transport";
import { Modality, Provider } from "@cyclo/provider/contract";
import {
  createResourceExhaustedError,
  resourceExhaustedRetryAt,
} from "@cyclo/provider/errors";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { aggregateUsageFile } from "../src/audit.mjs";
import { buildCatalogue } from "../src/catalogue.mjs";
import { login } from "../src/login.mjs";
import { createPiAdapter } from "../src/pi-adapter.mjs";
import { createGatewayServer } from "../src/server.mjs";
import { createGatewayServices } from "../src/services.mjs";

test("routes only on model and passes the opaque payload to the endpoint", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-services-"));
  const model = publicModel("work/gpt-test");
  const route = {
    account: "work",
    provider: "openai",
    publicModel: model,
    rawModel: { id: "gpt-test", provider: "openai", api: "openai-responses" },
  };
  const calls = [];
  const audit = [];
  const requestPayload = " { \"context\": {\"tools\":[{\"anyOf\":[]}]}, \"x\": 1 } ";
  const responsePayloads = [
    "{\"type\":\"start\",\"partial\":{}}",
    " { \"type\": \"future_pi_event\", \"unknown\": true } ",
    "{\"type\":\"done\",\"message\":{}}",
  ];
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.freeze(Object.assign(Object.create(null), { [model.id]: route })),
    },
    credentials: { async resolve(selected) {
      calls.push(["credential", selected.publicModel.id]);
      return { apiKey: "private-key" };
    } },
    backend: { infer(selected, payload, credential, signal) {
      calls.push(["infer", selected.publicModel.id, payload, credential.apiKey, signal.aborted]);
      return backendStream(responsePayloads);
    } },
    audit: { async record(value) { audit.push(value); } },
  });
  const server = await createGatewayServer({ services });

  try {
    const target = await listenTarget(server);
    const client = createClient(Provider, createDockerTransport(target));
    const payloads = [];
    for await (const response of client.infer(
      { model: model.id, payload: requestPayload },
      { headers: {
        authorization: "Bearer hostile-caller-token",
        "x-api-key": "hostile-key",
        cookie: "hostile-cookie",
      } },
    )) payloads.push(response.payload);

    assert.deepEqual(payloads, responsePayloads);
    assert.deepEqual(calls, [
      ["credential", model.id],
      ["infer", model.id, requestPayload, "private-key", false],
    ]);
    assert.equal(audit.at(-1).outcome, "ok");
    assert.equal(audit.at(-1).input_tokens, 7);
  } finally {
    await closeComponentServer(server);
    await rm(directory, { recursive: true, force: true });
  }
});

test("commits usage before releasing the final opaque response", async () => {
  const model = publicModel("work/gpt-test");
  const auditStarted = deferred();
  const releaseAudit = deferred();
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.assign(Object.create(null), {
        [model.id]: { publicModel: model, rawModel: {} },
      }),
    },
    credentials: { async resolve() { return { apiKey: "key" }; } },
    backend: {
      async *infer() {
        yield { payload: "streaming" };
        yield {
          payload: "terminal",
          usage: { inputTokens: 7, outputTokens: 3 },
        };
      },
    },
    audit: {
      async record(value) {
        auditStarted.resolve(value);
        await releaseAudit.promise;
      },
    },
  });
  const iterator = services.provider.infer(
    { model: model.id, payload: "opaque" },
    { signal: new AbortController().signal },
  )[Symbol.asyncIterator]();

  assert.deepEqual(await iterator.next(), {
    value: { payload: "streaming" },
    done: false,
  });
  const terminal = iterator.next();
  let terminalSettled = false;
  void terminal.then(
    () => { terminalSettled = true; },
    () => { terminalSettled = true; },
  );
  const record = await auditStarted.promise;
  assert.equal(record.outcome, "ok");
  assert.equal(record.input_tokens, 7);
  await Promise.resolve();
  assert.equal(terminalSettled, false);

  releaseAudit.resolve();
  assert.deepEqual(await terminal, {
    value: { payload: "terminal" },
    done: false,
  });
  assert.deepEqual(await iterator.next(), { value: undefined, done: true });
});

test("preserves typed pre-stream exhaustion through audit and ConnectRPC", async () => {
  const retryAt = new Date("2031-02-03T04:05:06.789Z");
  const model = publicModel("work/gpt-test");
  const audit = [];
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.assign(Object.create(null), {
        [model.id]: { publicModel: model, rawModel: {} },
      }),
    },
    credentials: { async resolve() { return { apiKey: "key" }; } },
    backend: {
      async *infer() {
        throw createResourceExhaustedError(retryAt);
      },
    },
    audit: { async record(value) { audit.push(value); } },
  });
  const server = await createGatewayServer({ services });

  try {
    const target = await listenTarget(server);
    const client = createClient(Provider, createDockerTransport(target));
    await assert.rejects(
      collect(client.infer({ model: model.id, payload: "opaque" })),
      (error) => error instanceof ConnectError
        && error.code === Code.ResourceExhausted
        && resourceExhaustedRetryAt(error)?.toISOString() === retryAt.toISOString(),
    );
    assert.equal(audit.length, 1);
    assert.equal(audit[0].outcome, `rpc_${Code.ResourceExhausted}`);
  } finally {
    await closeComponentServer(server);
  }
});

test("never returns a gateway credential reflected by a native upstream", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-secret-boundary-"));
  const authPath = join(directory, "auth.json");
  const modelsPath = join(directory, "models.json");
  const usagePath = join(directory, "usage.jsonl");
  const secret = "test-private-secret-9e0a";
  let receivedAuthorization;
  const upstream = createServer((request, response) => {
    receivedAuthorization = request.headers.authorization;
    response.writeHead(401, { "content-type": "application/json" });
    response.end(JSON.stringify({
      error: {
        message: `upstream rejected ${receivedAuthorization}`,
        type: "invalid_request_error",
      },
    }));
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  const { port } = upstream.address();

  await writeFile(authPath, JSON.stringify({
    work: { type: "api_key", provider: "echo", key: secret },
  }), { mode: 0o600 });
  await writeFile(modelsPath, JSON.stringify({
    providers: {
      echo: {
        api: "openai-responses",
        baseUrl: `http://127.0.0.1:${port}/v1`,
        models: [{
          id: "gpt-test",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        }],
      },
    },
  }), { mode: 0o600 });

  const services = createGatewayServices({
    env: {
      CYCLO_GATEWAY_AUTH_JSON: authPath,
      CYCLO_GATEWAY_MODELS_JSON: modelsPath,
      CYCLO_GATEWAY_USAGE_JSONL: usagePath,
    },
  });
  const server = await createGatewayServer({ services });

  try {
    const target = await listenTarget(server);
    const client = createClient(Provider, createDockerTransport(target));
    const payloads = [];
    let failure;
    try {
      for await (const response of client.infer({
        model: "work/gpt-test",
        payload: JSON.stringify({
          context: {
            messages: [{ role: "user", content: "hello", timestamp: 0 }],
          },
          options: { maxTokens: 32 },
        }),
      })) payloads.push(response.payload);
    } catch (error) {
      failure = error;
    }

    assert.equal(receivedAuthorization, `Bearer ${secret}`);
    assert.equal(payloads.length, 0);
    assert.ok(failure instanceof ConnectError);
    assert.equal(failure.code, Code.DataLoss);
    const visibleFailure = [
      failure.message,
      failure.rawMessage,
      ...Array.from(failure.metadata.entries()).flat(),
    ].join("\n");
    assert.equal(visibleFailure.includes(secret), false);
    assert.equal(visibleFailure.includes(`Bearer ${secret}`), false);
  } finally {
    await closeComponentServer(server);
    await new Promise((resolve, reject) => upstream.close((error) => (
      error ? reject(error) : resolve()
    )));
    await rm(directory, { recursive: true, force: true });
  }
});

test("never reconstructs a gateway credential split across native events", async () => {
  const secret = "test-private-secret-9e0a";
  const model = publicModel("work/gpt-test");
  const events = [
    { type: "text_delta", delta: "test-private-" },
    { type: "text_delta", delta: "secret-" },
    { type: "text_delta", delta: "9e0a" },
    { type: "done", message: {} },
  ];
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.assign(Object.create(null), {
        [model.id]: {
          publicModel: model,
          rawModel: {
            id: "gpt-test",
            provider: "openai",
            api: "openai-responses",
          },
        },
      }),
    },
    credentials: {
      async resolve() {
        return { apiKey: secret, secretValues: [secret] };
      },
    },
    backend: createPiAdapter({
      streamers: {
        "openai-responses": () => backendEvents(events),
      },
    }),
    audit: { async record() {} },
  });

  const payloads = [];
  let failure;
  try {
    for await (const response of services.provider.infer(
      {
        model: model.id,
        payload: JSON.stringify({ context: {}, options: {} }),
      },
      { signal: new AbortController().signal },
    )) payloads.push(response.payload);
  } catch (error) {
    failure = error;
  }

  const visibleText = payloads
    .map((payload) => JSON.parse(payload).delta ?? "")
    .join("");
  assert.equal(visibleText.includes(secret), false);
  assert.ok(failure instanceof ConnectError);
  assert.equal(failure.code, Code.DataLoss);
  assert.equal(failure.rawMessage.includes(secret), false);
});

test("one public ID survives login, catalogue, inference, audit, and usage", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-route-contract-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const authPath = join(directory, "auth.json");
  const modelsPath = join(directory, "models.json");
  const usagePath = join(directory, "usage.jsonl");
  const account = "a".repeat(64);
  const localModel = "😀".repeat(512);
  const publicId = `${account}/${localModel}`;
  const apiProvider = {
    api: "openai-responses",
    stream() {},
    streamSimple() {},
  };
  const providers = [{
    id: "openai",
    auth: { apiKey: {} },
    getModels() { return []; },
    streamSimple() {},
  }];
  await writeFile(authPath, "{}\n", { mode: 0o600 });
  await writeFile(modelsPath, JSON.stringify({
    providers: {
      custom: {
        api: "openai-responses",
        baseUrl: "https://example.invalid/v1",
        models: [{
          id: localModel,
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        }],
      },
    },
  }), { mode: 0o600 });

  await login(
    ["custom", "--as", account, "--api-key-env", "TEST_KEY"],
    {
      env: {
        CYCLO_GATEWAY_AUTH_JSON: authPath,
        CYCLO_GATEWAY_MODELS_JSON: modelsPath,
        TEST_KEY: "test-only-key",
      },
      output: { write() {} },
      providers,
      getApiProvider: () => apiProvider,
    },
  );
  const catalogue = buildCatalogue({
    authPath,
    modelsPath,
    providers,
    getApiProvider: () => apiProvider,
  });
  assert.deepEqual(catalogue.models.map(({ id }) => id), [publicId]);

  const services = createGatewayServices({
    env: {
      CYCLO_GATEWAY_AUTH_JSON: authPath,
      CYCLO_GATEWAY_MODELS_JSON: modelsPath,
      CYCLO_GATEWAY_USAGE_JSONL: usagePath,
    },
    catalogue,
    backend: {
      async *infer(_route, payload, credential) {
        assert.equal(payload, "opaque");
        assert.equal(credential.apiKey, "test-only-key");
        yield {
          payload: "response",
          usage: { inputTokens: 2, outputTokens: 3 },
        };
      },
    },
  });
  const responses = [];
  for await (const response of services.provider.infer(
    { model: publicId, payload: "opaque" },
    { signal: new AbortController().signal },
  )) responses.push(response.payload);
  assert.deepEqual(responses, ["response"]);

  const usage = await aggregateUsageFile(usagePath);
  assert.equal(usage.by_provider[account].requests, 1);
  assert.equal(usage.by_model[publicId].total_tokens, 5);
});

test("records client abandonment without requiring inference semantics", async () => {
  const model = publicModel("work/gpt-test");
  const audit = [];
  const services = createGatewayServices({
    catalogue: {
      models: [model],
      routes: Object.assign(Object.create(null), {
        [model.id]: { publicModel: model, rawModel: {} },
      }),
    },
    credentials: { async resolve() { return { apiKey: "key" }; } },
    backend: { infer() { return endlessBackend(); } },
    audit: { async record(value) { audit.push(value); } },
  });
  const iterator = services.provider.infer(
    { model: model.id, payload: "opaque" },
    { signal: new AbortController().signal },
  )[Symbol.asyncIterator]();
  assert.equal((await iterator.next()).value.payload, "first");
  await iterator.return();
  assert.equal(audit.at(-1).outcome, "client_abandoned");
});

test("startup logs models excluded from the usable catalogue", () => {
  const warnings = [];
  const services = createGatewayServices({
    catalogue: {
      models: [],
      routes: Object.freeze(Object.create(null)),
      diagnostics: [{
        account: "work",
        model: "broken",
        message: "private diagnostic",
      }],
    },
    credentials: { check() {} },
    backend: {},
    audit: { check() {} },
    warn(message) { warnings.push(message); },
  });

  assert.deepEqual(warnings, [
    "Cyclo gateway excluded unusable catalogue entry work/broken: private diagnostic",
  ]);
  assert.deepEqual(services.component.health({}), {
    status: HealthStatus.READY,
    message: "ready",
  });
});

test("startup catalogue diagnostics cannot inject multiline log entries", () => {
  const warnings = [];
  createGatewayServices({
    catalogue: {
      models: [],
      routes: Object.freeze(Object.create(null)),
      diagnostics: [{
        account: "work",
        model: "bad\nmodel\u0007",
        message: "invalid\ncatalogue\u0007entry",
      }],
    },
    credentials: { check() {} },
    backend: {},
    audit: { check() {} },
    warn(message) { warnings.push(message); },
  });

  assert.deepEqual(warnings, [
    "Cyclo gateway excluded unusable catalogue entry work/bad model: invalid catalogue entry",
  ]);
});

test("default construction publishes compatible Pi models", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-default-"));
  const authPath = join(directory, "auth.json");
  const modelsPath = join(directory, "models.json");
  try {
    await writeFile(authPath, JSON.stringify({
      openai: { type: "api_key", provider: "openai", key: "test-only-key" },
    }), { mode: 0o600 });
    await writeFile(modelsPath, "{}\n", { mode: 0o600 });
    const services = createGatewayServices({
      env: {
        CYCLO_GATEWAY_AUTH_JSON: authPath,
        CYCLO_GATEWAY_MODELS_JSON: modelsPath,
        CYCLO_GATEWAY_USAGE_JSONL: join(directory, "usage.jsonl"),
      },
    });
    const models = services.provider.listModels().models;
    assert.ok(models.length > 0);
    assert.ok(models.every(({ id }) => id.startsWith("openai/")));
    assert.ok(models.every(({ inferenceFormat }) => inferenceFormat === PI_INFERENCE_FORMAT));

    await writeFile(authPath, JSON.stringify({
      openai: { type: "api_key", provider: "openai", key: "test-only-key" },
      work: { type: "api_key", provider: "openai", key: "second-test-only-key" },
    }), { mode: 0o600 });
    assert.equal(services.component.health({}).status, HealthStatus.NOT_READY);

    await writeFile(authPath, "not json\n", { mode: 0o600 });
    assert.equal(services.component.health({}).status, HealthStatus.NOT_READY);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

function publicModel(id) {
  return Object.freeze({
    id,
    displayName: id,
    capabilities: Object.freeze({
      inputModalities: Object.freeze([Modality.TEXT]),
      outputModalities: Object.freeze([Modality.TEXT]),
      functionTools: true,
      parallelToolCalls: true,
      extensionTypes: Object.freeze([]),
    }),
    extensions: Object.freeze([]),
    inferenceFormat: PI_INFERENCE_FORMAT,
  });
}

async function* backendStream(payloads) {
  yield { payload: payloads[0] };
  yield { payload: payloads[1] };
  yield {
    payload: payloads[2],
    usage: { inputTokens: 7, outputTokens: 3, cachedInputTokens: 2, reasoningTokens: 1 },
  };
}

async function* endlessBackend() {
  yield { payload: "first" };
  yield { payload: "held" };
  await new Promise(() => {});
}

async function* backendEvents(events) {
  for (const event of events) yield event;
}

async function collect(values) {
  const result = [];
  for await (const value of values) result.push(value);
  return result;
}

async function listenTarget(server) {
  const address = await listenComponentServer(server, {
    host: "127.0.0.1",
    port: 0,
  });
  return `dns:///127.0.0.1:${address.port}`;
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
