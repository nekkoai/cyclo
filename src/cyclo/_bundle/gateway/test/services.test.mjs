import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createClient } from "@connectrpc/connect";
import { MessageRole, Modality, FinishReason, Provider } from "@cyclo/provider/contract";
import { HealthStatus } from "@cyclo/component/contract";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { createUnixTransport } from "@cyclo/component/transport";

import { createGatewayServer } from "../src/server.mjs";
import { createGatewayServices } from "../src/services.mjs";

test("socket callers need no bearer and arbitrary RPC headers never become native credentials", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-services-"));
  const socketPath = join(directory, "gateway.sock");

  const model = publicModel("work/gpt-test");
  const hidden = publicModel("other/gpt-test");
  const route = {
    account: "work",
    provider: "openai",
    publicModel: model,
    rawModel: {
      id: "gpt-test",
      provider: "openai",
      api: "openai-responses",
      baseUrl: "https://example.invalid/v1",
    },
  };
  const calls = [];
  const audit = [];
  const services = createGatewayServices({
    catalogue: {
      models: [model, hidden],
      routes: Object.freeze(Object.assign(Object.create(null), {
        [model.id]: Object.freeze(route),
      })),
    },
    credentials: { async resolve(selected) {
      calls.push(["credential", selected.publicModel.id]);
      return { apiKey: "private-key", sensitiveValues: ["private-key"] };
    } },
    backend: { infer(...arguments_) {
      assert.equal(arguments_.length, 4);
      const [selected, prepared, credential, signal] = arguments_;
      assert.deepEqual(credential, {
        apiKey: "private-key",
        sensitiveValues: ["private-key"],
      });
      assert.doesNotMatch(
        JSON.stringify([selected, prepared, credential]),
        /hostile-caller-token|hostile-key|hostile-cookie/u,
      );
      calls.push(["infer", selected.publicModel.id, prepared.context.messages[0].content, credential.apiKey, signal.aborted]);
      return responseStream(selected.publicModel.id);
    } },
    audit: { async record(value) { audit.push(value); } },
  });
  const server = await createGatewayServer({ services });

  try {
    await listenComponentServer(server, { socketPath });
    const client = createClient(Provider, createUnixTransport(socketPath));
    const hostileHeaders = new Headers({
      authorization: "Bearer hostile-caller-token",
      "x-api-key": "hostile-key",
      cookie: "credential=hostile-cookie",
    });

    const catalogue = await client.listModels({});
    assert.deepEqual(catalogue.models.map(({ id }) => id), [model.id, hidden.id]);
    const catalogueWithHeaders = await client.listModels({}, { headers: hostileHeaders });
    assert.deepEqual(catalogueWithHeaders.models.map(({ id }) => id), [model.id, hidden.id]);

    const events = [];
    for await (const response of client.infer(inferenceRequest(), { headers: hostileHeaders })) {
      events.push(response.event.case);
    }
    assert.deepEqual(events, ["started", "itemStarted", "itemDelta", "itemFinished", "finished"]);
    assert.deepEqual(calls, [
      ["credential", model.id],
      ["infer", model.id, "hello", "private-key", false],
    ]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(audit.at(-1).outcome, "ok");

    const abandoned = services.provider.infer(inferenceRequest(), {
      signal: new AbortController().signal,
    })[Symbol.asyncIterator]();
    assert.equal((await abandoned.next()).value.event.case, "started");
    await abandoned.return();
    assert.equal(audit.at(-1).outcome, "client_abandoned");
  } finally {
    await closeComponentServer(server);
    await rm(directory, { recursive: true, force: true });
  }
});

test("default construction binds the pinned pi-ai catalogue and adapters", async () => {
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
    const catalogue = services.provider.listModels();
    assert.ok(catalogue.models.length > 0);
    assert.ok(catalogue.models.every(({ id }) => id.startsWith("openai/")));

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
  });
}

function inferenceRequest() {
  return {
    model: "work/gpt-test",
    input: [{
      item: {
        case: "message",
        value: {
          role: MessageRole.USER,
          content: [{ content: { case: "text", value: "hello" } }],
        },
      },
    }],
  };
}

async function* responseStream(model) {
  yield { event: { case: "started", value: { responseId: "response", model } } };
  yield { event: { case: "itemStarted", value: { index: 0, item: { case: "text", value: {} } } } };
  yield { event: { case: "itemDelta", value: { index: 0, delta: { case: "text", value: "ok" } } } };
  yield { event: { case: "itemFinished", value: { index: 0 } } };
  yield { event: { case: "finished", value: { reason: FinishReason.STOP } } };
}
