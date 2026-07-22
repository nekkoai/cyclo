import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createClient } from "@connectrpc/connect";
import { HealthStatus } from "@cyclo/component/contract";
import { closeComponentServer, listenComponentServer } from "@cyclo/component/server";
import { createUnixTransport } from "@cyclo/component/transport";
import { Modality, Provider } from "@cyclo/provider/contract";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { createGatewayServer } from "../src/server.mjs";
import { createGatewayServices } from "../src/services.mjs";

test("routes only on model and passes the opaque payload to the endpoint", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-services-"));
  const socketPath = join(directory, "gateway.sock");
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
    await listenComponentServer(server, { socketPath });
    const client = createClient(Provider, createUnixTransport(socketPath));
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
  await new Promise(() => {});
}
