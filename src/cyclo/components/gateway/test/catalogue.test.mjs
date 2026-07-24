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
    providers: [piProvider("openai", [])],
    getApiProvider: (api) => apiProviders.get(api),
    ...overrides,
  };
}

function piProvider(id, models, options = {}) {
  return {
    id,
    baseUrl: options.baseUrl,
    auth: options.auth ?? { apiKey: {} },
    getModels: () => models,
    ...(options.filterModels ? { filterModels: options.filterModels } : {}),
    streamSimple() {},
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
          contextWindow: 8192,
          maxTokens: 2048,
        },
        {
          id: "ordinary",
          reasoning: false,
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
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
      providers: [piProvider("openai", [{
          id: "gpt",
          name: "GPT",
          api: "openai-responses",
          reasoning: false,
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        }], { baseUrl: "https://api.openai.invalid/v1" })],
    }),
  });
  assert.equal(catalogue.models[0].id, "openai/gpt");
  assert.equal(catalogue.routes["openai/gpt"].provider, "openai");
});

test("malformed configuration fails closed and bad models are isolated", async (t) => {
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
        contextWindow: 4096,
        maxTokens: 1024,
      }],
    },
  });
  const unavailableCatalogue = buildCatalogue({
    ...unavailable,
    ...dependencies({ getApiProvider: () => undefined }),
  });
  assert.deepEqual(unavailableCatalogue.models, []);
  assert.match(unavailableCatalogue.diagnostics[0].message, /no API implementation/u);

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
  const secretCatalogue = buildCatalogue({ ...secretHeader, ...dependencies() });
  assert.deepEqual(secretCatalogue.models, []);
  assert.match(secretCatalogue.diagnostics[0].message, /must not define headers/u);
});

test("account prefixes and local model IDs obey the public route contract", async (t) => {
  const invalidAccount = await fixture(t, {
    _legacy: { type: "api_key", provider: "openai", key: "key" },
  }, {});
  assert.throws(
    () => buildCatalogue({ ...invalidAccount, ...dependencies() }),
    /account name/u,
  );

  const invalidProvider = await fixture(t, {}, {
    "-legacy": {
      api: "openai-responses",
      baseUrl: "https://example.invalid/v1",
      models: [],
    },
  });
  assert.throws(
    () => buildCatalogue({ ...invalidProvider, ...dependencies() }),
    /custom provider name/u,
  );

  const account = "a".repeat(64);
  const boundaryModel = "😀".repeat(512);
  const paths = await fixture(t, {
    [account]: { type: "api_key", provider: "custom", key: "key" },
  }, {
    custom: {
      api: "openai-responses",
      baseUrl: "https://example.invalid/v1",
      models: [
        {
          id: boundaryModel,
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
        {
          id: "bad\u0085model",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
        {
          id: "😀".repeat(513),
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
        {
          id: "\ud800",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
      ],
    },
  });

  const catalogue = buildCatalogue({ ...paths, ...dependencies() });
  assert.deepEqual(catalogue.models.map(({ id }) => id), [
    `${account}/${boundaryModel}`,
  ]);
  assert.equal(catalogue.diagnostics.length, 3);
  assert.ok(catalogue.diagnostics.every(({ message }) => /invalid model id/u.test(message)));
});

test("a model without Pi token limits cannot hide valid models", async (t) => {
  const paths = await fixture(t, {
    work: { type: "api_key", provider: "custom", key: "key" },
  }, {
    custom: {
      api: "openai-responses",
      baseUrl: "https://example.invalid/v1",
      models: [
        {
          id: "broken",
          input: ["text"],
          contextWindow: 4096,
        },
        {
          id: "usable",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
      ],
    },
  });
  const catalogue = buildCatalogue({ ...paths, ...dependencies() });

  assert.deepEqual(catalogue.models.map(({ id }) => id), ["work/usable"]);
  assert.deepEqual(catalogue.diagnostics.map(({ model }) => model), ["broken"]);
  assert.match(catalogue.diagnostics[0].message, /no usable output limit/u);
});

test("provider failures are isolated without exposing exception text", async (t) => {
  const paths = await fixture(t, {
    broken: { type: "api_key", provider: "broken", key: "key" },
    work: { type: "api_key", provider: "known", key: "key" },
  }, {});
  const broken = piProvider("broken", [], {
    baseUrl: "https://broken.invalid/v1",
  });
  broken.getModels = () => {
    throw new Error("provider leaked private-key");
  };
  const catalogue = buildCatalogue({
    ...paths,
    ...dependencies({
      providers: [
        broken,
        piProvider("known", [{
          id: "usable",
          api: "openai-responses",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        }], { baseUrl: "https://known.invalid/v1" }),
      ],
    }),
  });

  assert.deepEqual(catalogue.models.map(({ id }) => id), ["work/usable"]);
  assert.equal(
    catalogue.diagnostics[0].message,
    "provider broken model discovery failed",
  );
  assert.doesNotMatch(JSON.stringify(catalogue.diagnostics), /private-key/u);
});

test("provider-owned OAuth filtering limits an account's model catalogue", async (t) => {
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
      providers: [piProvider("github-copilot", [
        {
          id: "kept",
          api: "openai-responses",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
        {
          id: "hidden",
          api: "openai-responses",
          input: ["text"],
          contextWindow: 4096,
          maxTokens: 1024,
        },
      ], {
        baseUrl: "https://default.invalid/v1",
        auth: { oauth: {} },
        filterModels: (models, credential) => models
          .filter(({ id }) => credential.availableModelIds.includes(id)),
      })],
    }),
  });
  assert.deepEqual(catalogue.models.map(({ id }) => id), ["copilot/kept"]);
  assert.equal(catalogue.routes["copilot/kept"].rawModel.baseUrl, "https://default.invalid/v1");
});
