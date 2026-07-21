import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { buildCatalogue } from "../src/catalogue.mjs";

const apiProviders = new Map(
  ["openai-responses", "openai-codex-responses", "anthropic-messages"].map((api) => [
    api,
    { api, stream() {}, streamSimple() {} },
  ]),
);

function dependencies(overrides = {}) {
  return {
    getBuiltinProviders: () => [],
    getBuiltinModels: () => [],
    getApiProvider: (api) => apiProviders.get(api),
    ...overrides,
  };
}

async function fixture(t, auth, providers) {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-catalogue-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const authPath = join(root, "auth.json");
  const modelsPath = join(root, "models.json");
  await writeFile(authPath, JSON.stringify(auth));
  await writeFile(modelsPath, JSON.stringify({ providers }));
  return { authPath, modelsPath };
}

test("the catalogue publishes account/model IDs and keeps native routes private", async (t) => {
  const paths = await fixture(t, {
    work: { type: "api_key", provider: "custom", key: "credential-secret" },
  }, {
    custom: {
      api: "openai-responses",
      baseUrl: "https://default.invalid/v1",
      models: [
        {
          id: "fast",
          name: "Fast",
          api: "openai-responses",
          baseUrl: "https://one.invalid/v1",
          reasoning: false,
          input: ["text", "image"],
          compat: { supportsTemperature: false },
          contextWindow: 4096,
          maxTokens: 1024,
        },
        {
          id: "reasoning",
          api: "anthropic-messages",
          baseUrl: "https://two.invalid/v1",
          reasoning: true,
          input: ["text"],
        },
        {
          id: "ordinary",
          reasoning: false,
          input: ["text"],
        },
        {
          id: "not-yet-supported",
          api: "openai-completions",
          baseUrl: "https://three.invalid/v1",
          reasoning: false,
          input: ["text"],
        },
      ],
    },
  });
  const catalogue = buildCatalogue({ ...paths, ...dependencies() });

  assert.deepEqual(catalogue.models.map((model) => model.id), [
    "work/fast",
    "work/ordinary",
    "work/reasoning",
  ]);
  const fast = catalogue.models[0];
  assert.deepEqual(fast.capabilities.inputModalities, [1, 2]);
  assert.deepEqual(fast.capabilities.outputModalities, [1]);
  assert.equal(fast.capabilities.functionTools, true);
  assert.equal(fast.capabilities.parallelToolCalls, true);
  assert.equal(fast.capabilities.reasoningSummaries, false);
  assert.equal(fast.capabilities.temperature, false);
  assert.equal(fast.capabilities.topP, false);
  assert.equal(fast.capabilities.stopSequences, false);
  assert.deepEqual(fast.capabilities.extensionTypes, []);
  assert.equal(fast.contextWindowTokens, 4096n);
  assert.equal(fast.maxOutputTokens, 1024n);
  assert.equal(catalogue.models[1].capabilities.temperature, true);
  assert.equal(catalogue.models[2].capabilities.temperature, false);

  assert.equal(catalogue.routes["work/fast"].baseUrl, "https://one.invalid/v1");
  assert.equal(catalogue.routes["work/reasoning"].baseUrl, "https://two.invalid/v1");
  assert.equal(catalogue.routes["work/ordinary"].baseUrl, "https://default.invalid/v1");
  assert.equal(catalogue.routes["work/fast"].api, "openai-responses");
  assert.equal(catalogue.routes["work/reasoning"].api, "anthropic-messages");
  assert.equal(catalogue.routes["work/fast"].publicModel, fast);
  assert.equal(catalogue.routes["work/fast"].rawModel.baseUrl, "https://one.invalid/v1");
  assert.equal(Object.isFrozen(catalogue), true);
  assert.equal(Object.isFrozen(catalogue.models), true);
  assert.equal(Object.isFrozen(catalogue.routes), true);
  const publicJson = JSON.stringify(
    catalogue.models,
    (_key, value) => typeof value === "bigint" ? value.toString() : value,
  );
  assert.equal(publicJson.includes("credential-secret"), false);
  assert.equal(publicJson.includes("one.invalid"), false);
});

test("legacy credentials default their provider type to the account name", async (t) => {
  const paths = await fixture(t, {
    openai: { type: "api_key", key: "key" },
  }, {});
  const catalogue = buildCatalogue({
    ...paths,
    ...dependencies({
      getBuiltinProviders: () => ["openai"],
      getBuiltinModels: () => [{
        id: "gpt",
        name: "GPT",
        api: "openai-responses",
        baseUrl: "https://api.openai.invalid/v1",
        reasoning: false,
        input: ["text"],
      }],
    }),
  });
  assert.equal(catalogue.models[0].id, "openai/gpt");
  assert.equal(catalogue.routes["openai/gpt"].provider, "openai");
});

test("malformed configuration and missing API implementations fail closed", async (t) => {
  const badUrl = await fixture(t, {
    work: { type: "api_key", provider: "custom", key: "key" },
  }, {
    custom: {
      api: "openai-responses",
      baseUrl: "https://user:secret@example.invalid/v1",
      models: [],
    },
  });
  assert.throws(() => buildCatalogue({ ...badUrl, ...dependencies() }), /baseUrl/u);

  const unavailable = await fixture(t, {
    work: { type: "api_key", provider: "custom", key: "key" },
  }, {
    custom: {
      models: [{
        id: "gpt",
        api: "openai-responses",
        baseUrl: "https://example.invalid/v1",
        reasoning: false,
        input: ["text"],
      }],
    },
  });
  assert.throws(
    () => buildCatalogue({
      ...unavailable,
      ...dependencies({ getApiProvider: () => undefined }),
    }),
    /no API implementation/u,
  );

  const secretHeader = await fixture(t, {
    work: { type: "api_key", provider: "custom", key: "key" },
  }, {
    custom: {
      api: "openai-responses",
      baseUrl: "https://example.invalid/v1",
      models: [{
        id: "gpt",
        input: ["text"],
        headers: { authorization: "must-not-live-here" },
      }],
    },
  });
  assert.throws(
    () => buildCatalogue({ ...secretHeader, ...dependencies() }),
    /must not define headers/u,
  );
});

test("OAuth catalogue modifiers filter and specialize account models", async (t) => {
  const paths = await fixture(t, {
    copilot: {
      type: "oauth",
      provider: "github-copilot",
      access: "access-token",
      refresh: "refresh-token",
      expires: Date.now() + 3_600_000,
      availableModelIds: ["kept"],
    },
  }, {});
  const catalogue = buildCatalogue({
    ...paths,
    ...dependencies({
      getBuiltinProviders: () => ["github-copilot"],
      getBuiltinModels: () => [
        {
          id: "kept",
          api: "openai-responses",
          baseUrl: "https://default.invalid/v1",
          input: ["text"],
        },
        {
          id: "hidden",
          api: "openai-responses",
          baseUrl: "https://default.invalid/v1",
          input: ["text"],
        },
      ],
      getOAuthProvider: () => ({
        modifyModels: (models, credential) => models
          .filter(({ id }) => credential.availableModelIds.includes(id))
          .map((model) => ({ ...model, baseUrl: "https://account.invalid/v1" })),
      }),
    }),
  });
  assert.deepEqual(catalogue.models.map(({ id }) => id), ["copilot/kept"]);
  assert.equal(catalogue.routes["copilot/kept"].rawModel.baseUrl, "https://account.invalid/v1");
  assert.equal(catalogue.routes["copilot/kept"].sourceModel.baseUrl, "https://default.invalid/v1");
});
