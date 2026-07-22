import assert from "node:assert/strict";
import test from "node:test";

import {
  discoverSupportedProviders,
  formatSupportedProviders,
} from "../src/providers.mjs";

const DESCRIPTIONS = Object.freeze({
  anthropic: "Anthropic models",
  openai: "OpenAI models",
  "openai-codex": "OpenAI subscription",
});

function piProvider(id, api, { apiKey = true, oauth = false } = {}) {
  return {
    id,
    auth: {
      ...(apiKey ? { apiKey: {} } : {}),
      ...(oauth ? { oauth: {} } : {}),
    },
    getModels: () => [{ api }],
    streamSimple() {},
  };
}

test("provider discovery describes copyable pre-login choices", () => {
  const providers = discoverSupportedProviders({
    providers: [
      piProvider("openai-codex", "openai-responses", { apiKey: false, oauth: true }),
      piProvider("openai", "openai-responses"),
      piProvider("anthropic", "anthropic-messages", { oauth: true }),
    ],
    descriptions: DESCRIPTIONS,
    exposedApis: new Set(["anthropic-messages", "openai-responses"]),
  });

  assert.deepEqual(providers, [
    {
      provider: "anthropic",
      description: "Anthropic models",
      auth: "oauth or api-key",
      login: "cyclo gateway login anthropic",
    },
    {
      provider: "openai",
      description: "OpenAI models",
      auth: "api-key",
      login: "cyclo gateway login openai --api-key-stdin",
    },
    {
      provider: "openai-codex",
      description: "OpenAI subscription",
      auth: "oauth",
      login: "cyclo gateway login openai-codex",
    },
  ]);
  const output = formatSupportedProviders(providers);
  assert.match(output, /Supported gateway providers/u);
  assert.match(output, /PROVIDER\s+DESCRIPTION\s+AUTH\s+LOGIN COMMAND/u);
  assert.match(output, /anthropic\s+Anthropic models\s+oauth or api-key/u);
  assert.match(output, /openai-codex\s+OpenAI subscription\s+oauth/u);
  assert.match(output, /Use --as NAME/u);
});

test("provider discovery rejects unsafe or unexplained registries", () => {
  for (const providers of [
    [],
    [piProvider("openai", "openai-responses"), piProvider("openai", "openai-responses")],
    [piProvider("bad.provider", "openai-responses")],
    [piProvider("bad\tprovider", "openai-responses")],
    [42],
  ]) {
    assert.throws(
      () => discoverSupportedProviders({
        providers,
        descriptions: DESCRIPTIONS,
        exposedApis: new Set(["openai-responses"]),
      }),
      /invalid provider registry/u,
    );
  }
  assert.throws(
    () => discoverSupportedProviders({
      providers: [piProvider("future-provider", "openai-responses")],
      descriptions: DESCRIPTIONS,
      exposedApis: new Set(["openai-responses"]),
    }),
    /has no safe description/u,
  );
  assert.throws(
    () => discoverSupportedProviders({
      providers: [piProvider("openai", "openai-responses")],
      descriptions: { openai: "unsafe\tdescription" },
      exposedApis: new Set(["openai-responses"]),
    }),
    /has no safe description/u,
  );
});

test("the pinned pi-ai provider registry is fully described", () => {
  const providers = discoverSupportedProviders();
  assert.ok(providers.length > 0);
  assert.equal(new Set(providers.map(({ provider }) => provider)).size, providers.length);
  assert.doesNotMatch(formatSupportedProviders(providers), /undefined|null/u);
  assert.equal(providers.some(({ provider }) => provider === "google"), false);
});

test("provider discovery omits providers with no gateway-supported native API", () => {
  const providers = discoverSupportedProviders({
    providers: [
      piProvider("openai", "openai-responses"),
      piProvider("google", "google-generative-ai"),
    ],
    descriptions: { openai: "OpenAI", google: "Google" },
    exposedApis: new Set(["openai-responses"]),
  });
  assert.deepEqual(providers.map(({ provider }) => provider), ["openai"]);
});
