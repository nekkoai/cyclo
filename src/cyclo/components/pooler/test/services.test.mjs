import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createClient, Code, ConnectError } from "@connectrpc/connect";
import { resolveBindings } from "@cyclo/component/bindings";
import { Component, HealthStatus } from "@cyclo/component/contract";
import { parseDeclaration } from "@cyclo/component/declaration";
import {
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { createDockerTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";
import {
  createResourceExhaustedError,
  resourceExhaustedRetryAt,
} from "@cyclo/provider/errors";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { parseArguments } from "../src/config.mjs";
import { checkPoolerHealth } from "../src/healthcheck.mjs";
import { createPoolerServer } from "../src/server.mjs";
import { createPoolerServices } from "../src/services.mjs";

const EXACT_CONFIG = parseArguments([
  "account-one/model",
  "account-two/model",
  "model=balanced",
]);
const PAYLOAD = " { \"tools\": [{\"schema\": {\"anyOf\": []}}], \"future\": true } ";

test("declares Component and Provider with exactly one upstream Provider", async () => {
  const declaration = parseDeclaration(
    await readFile(new URL("../component.conf", import.meta.url), "utf8"),
  );
  assert.deepEqual(declaration, {
    name: "pooler",
    provides: [Component.typeName, Provider.typeName],
    requires: [{ name: "upstream", service: Provider.typeName }],
  });
});

test("exact mode preserves upstream and round-robins the virtual model", async () => {
  const observed = [];
  await withPooler({
    async *infer(request) {
      observed.push({ model: request.model, payload: request.payload });
      yield { payload: `response:${request.model}` };
    },
  }, async ({ provider }) => {
    const listed = await provider.listModels({});
    assert.deepEqual(listed.models.map((entry) => entry.id), [
      "account-one/model",
      "account-two/model",
      "other/model",
      "pool/balanced",
    ]);
    assert.equal(listed.models[3].contextWindowTokens, 100_000n);
    assert.equal(listed.models[3].maxOutputTokens, 8_000n);

    assert.deepEqual(
      await collect(provider.infer({ model: "other/model", payload: PAYLOAD })),
      ["response:other/model"],
    );
    assert.deepEqual(
      await collect(provider.infer({ model: "pool/balanced", payload: PAYLOAD })),
      ["response:account-one/model"],
    );
    assert.deepEqual(
      await collect(provider.infer({ model: "pool/balanced", payload: PAYLOAD })),
      ["response:account-two/model"],
    );
  });
  assert.deepEqual(observed, [
    { model: "other/model", payload: PAYLOAD },
    { model: "account-one/model", payload: PAYLOAD },
    { model: "account-two/model", payload: PAYLOAD },
  ]);
});

test("provider-wide mode publishes shared models and shares provider cooldown", async () => {
  const retryAt = new Date(Date.now() + 60_000);
  const observed = [];
  const providerModels = [
    model("account-one/model", 200_000n, 8_000n),
    model("account-one/second", 80_000n, 4_000n),
    model("account-one/only", 50_000n, 2_000n),
    model("account-two/model", 100_000n, 12_000n),
    model("account-two/second", 90_000n, 6_000n),
  ];
  await withPooler({
    listModels: () => ({ models: providerModels }),
    async *infer(request) {
      observed.push(request.model);
      if (request.model === "account-one/model") {
        throw createResourceExhaustedError(retryAt);
      }
      yield { payload: `response:${request.model}` };
    },
  }, async ({ provider }) => {
    const listed = await provider.listModels({});
    assert.deepEqual(listed.models.map((entry) => entry.id), [
      "account-one/model",
      "account-one/second",
      "account-one/only",
      "account-two/model",
      "account-two/second",
      "pool/model",
      "pool/second",
    ]);
    assert.deepEqual(
      await collect(provider.infer({ model: "pool/model", payload: PAYLOAD })),
      ["response:account-two/model"],
    );
    assert.deepEqual(
      await collect(provider.infer({ model: "pool/second", payload: PAYLOAD })),
      ["response:account-two/second"],
    );
  }, parseArguments(["account-one", "account-two"]));
  assert.deepEqual(observed, [
    "account-one/model",
    "account-two/model",
    "account-two/second",
  ]);
});

test("typed pre-stream exhaustion retries, then returns earliest retry", async () => {
  const later = new Date(Date.now() + 120_000);
  const earlier = new Date(Date.now() + 60_000);
  const observed = [];
  await withPooler({
    async *infer(request) {
      observed.push(request.model);
      if (request.model === "account-one/model" && observed.length === 1) {
        throw createResourceExhaustedError(earlier);
      }
      yield { payload: "accepted" };
    },
  }, async ({ provider }) => {
    assert.deepEqual(
      await collect(provider.infer({ model: "pool/balanced", payload: PAYLOAD })),
      ["accepted"],
    );
  });
  assert.deepEqual(observed, ["account-one/model", "account-two/model"]);

  await withPooler({
    async *infer(request) {
      throw createResourceExhaustedError(
        request.model === "account-one/model" ? later : earlier,
      );
    },
  }, async ({ provider }) => {
    const error = await rejectedStream(
      provider.infer({ model: "pool/balanced", payload: PAYLOAD }),
    );
    assert.equal(error.code, Code.ResourceExhausted);
    assert.equal(resourceExhaustedRetryAt(error)?.getTime(), earlier.getTime());
  });
});

test("a response or ambiguous failure is never replayed", async () => {
  const responded = [];
  await withPooler({
    async *infer(request) {
      responded.push(request.model);
      yield { payload: "first" };
      throw createResourceExhaustedError(new Date(Date.now() + 60_000));
    },
  }, async ({ provider }) => {
    const iterator = provider.infer({ model: "pool/balanced", payload: PAYLOAD })[
      Symbol.asyncIterator
    ]();
    assert.equal((await iterator.next()).value.payload, "first");
    await assert.rejects(iterator.next(), (error) => {
      assert.equal(error.code, Code.ResourceExhausted);
      return true;
    });
  });
  assert.deepEqual(responded, ["account-one/model"]);

  const ambiguous = [];
  await withPooler({
    async *infer(request) {
      ambiguous.push(request.model);
      throw new ConnectError("ambiguous", Code.Unavailable);
    },
  }, async ({ provider }) => {
    await assert.rejects(
      collect(provider.infer({ model: "pool/balanced", payload: PAYLOAD })),
      (error) => error.code === Code.Unavailable,
    );
  });
  assert.deepEqual(ambiguous, ["account-one/model"]);
});

test("health and RPC failures expose the same catalogue diagnostic once", async () => {
  const diagnostics = [];
  const invalidConfig = parseArguments(["missing", "account-one"]);
  const expected = "pooler pool rejected the upstream catalogue: "
    + "configured member provider \"missing\" is unavailable; "
    + "configured providers: missing, account-one; "
    + "available upstream providers: account-one, account-two, other";

  await withPooler({}, async ({ component, provider, target }) => {
    assert.deepEqual(await component.health({}), {
      $typeName: "cyclo.component.v1.HealthResponse",
      status: HealthStatus.NOT_READY,
      message: expected,
    });
    await assert.rejects(provider.listModels({}), (error) => {
      assert.equal(error.code, Code.FailedPrecondition);
      assert.equal(error.rawMessage, expected);
      return true;
    });
    await assert.rejects(
      checkPoolerHealth({ target }),
      new Error(`pooler is not ready: ${expected}`),
    );
  }, invalidConfig, {
    logError(message) { diagnostics.push(message); },
  });
  assert.equal(diagnostics.length, 1);
  assert.match(diagnostics[0], /Caused by:\nTypeError:/u);
});

async function withPooler(overrides, run, config = EXACT_CONFIG, serviceOptions = {}) {
  const upstream = upstreamServer(overrides);
  let pooler;
  try {
    const upstreamTarget = await listenTarget(upstream);
    const upstreamClient = createClient(Provider, createDockerTransport(upstreamTarget));
    const services = createPoolerServices({
      upstream: {
        client: upstreamClient,
        callOptions(signal, timeoutMs) {
          const options = { signal };
          if (timeoutMs !== undefined) options.timeoutMs = timeoutMs;
          return options;
        },
      },
      config,
      componentName: "pool",
      healthTimeoutMs: 100,
      logError() {},
      ...serviceOptions,
    });
    pooler = await createPoolerServer({ services });
    const target = await listenTarget(pooler);
    const transport = createDockerTransport(target);
    await run({
      component: createClient(Component, transport),
      provider: createClient(Provider, transport),
      target,
    });
  } finally {
    if (pooler) await closeComponentServer(pooler).catch(() => {});
    await closeComponentServer(upstream).catch(() => {});
  }
}

function upstreamServer(overrides = {}) {
  const bindings = resolveBindings(parseDeclaration(`
    component upstream
    provide cyclo.component.v1.Component
    provide cyclo.provider.v1.Provider
  `), [Component, Provider]);
  const implementation = {
    listModels: () => ({ models: models() }),
    async *infer(request) { yield { payload: request.payload }; },
    ...overrides,
  };
  return createComponentServer({
    bindings,
    implementations: new Map([
      [Component.typeName, {
        health: () => ({ status: HealthStatus.READY, message: "ready" }),
      }],
      [Provider.typeName, implementation],
    ]),
  });
}

function models() {
  return [
    model("account-one/model", 200_000n, 8_000n),
    model("account-two/model", 100_000n, 12_000n),
    model("other/model", 50_000n, 4_000n),
  ];
}

function model(id, contextWindowTokens, maxOutputTokens) {
  return {
    id,
    displayName: id,
    capabilities: {
      inputModalities: [1],
      outputModalities: [1],
      functionTools: true,
      parallelToolCalls: true,
      reasoningSummaries: true,
      temperature: false,
      topP: false,
      stopSequences: false,
      extensionTypes: [],
      reasoning: true,
    },
    contextWindowTokens,
    maxOutputTokens,
    extensions: [],
    inferenceFormat: PI_INFERENCE_FORMAT,
  };
}

async function listenTarget(server) {
  const address = await listenComponentServer(server, {
    host: "127.0.0.1",
    port: 0,
  });
  return `dns:///127.0.0.1:${address.port}`;
}

async function collect(stream) {
  const responses = [];
  for await (const response of stream) responses.push(response.payload);
  return responses;
}

async function rejectedStream(stream) {
  try {
    await collect(stream);
  } catch (error) {
    return error;
  }
  assert.fail("stream unexpectedly succeeded");
}
