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

test("provider discovery describes copyable pre-login choices", () => {
  const providers = discoverSupportedProviders({
    builtinProviders: ["openai-codex", "openai", "anthropic"],
    builtinModels: (provider) => [{
      api: provider === "anthropic" ? "anthropic-messages" : "openai-responses",
    }],
    oauthProviders: [{ id: "anthropic" }, { id: "openai-codex" }],
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
      auth: "oauth or api-key",
      login: "cyclo gateway login openai-codex",
    },
  ]);
  assert.equal(
    formatSupportedProviders(providers),
    [
      "PROVIDER\tDESCRIPTION\tAUTH\tLOGIN",
      "anthropic\tAnthropic models\toauth or api-key\tcyclo gateway login anthropic",
      "openai\tOpenAI models\tapi-key\tcyclo gateway login openai --api-key-stdin",
      "openai-codex\tOpenAI subscription\toauth or api-key\tcyclo gateway login openai-codex",
      "",
      "Account/catalogue names default to PROVIDER. Add --as NAME to choose one; API-key login may always use --api-key-stdin.",
    ].join("\n"),
  );
});

test("provider discovery rejects unsafe or unexplained registries", () => {
  for (const builtinProviders of [
    [],
    ["openai", "openai"],
    ["bad.provider"],
    ["bad\tprovider"],
    [42],
  ]) {
    assert.throws(
      () => discoverSupportedProviders({
        builtinProviders,
        builtinModels: () => [{ api: "openai-responses" }],
        oauthProviders: [],
        descriptions: DESCRIPTIONS,
        exposedApis: new Set(["openai-responses"]),
      }),
      /invalid built-in provider registry/u,
    );
  }
  assert.throws(
    () => discoverSupportedProviders({
      builtinProviders: ["openai"],
      builtinModels: () => [{ api: "openai-responses" }],
      oauthProviders: [{ id: "openai" }, { id: "openai" }],
      descriptions: DESCRIPTIONS,
      exposedApis: new Set(["openai-responses"]),
    }),
    /invalid OAuth provider registry/u,
  );
  assert.throws(
    () => discoverSupportedProviders({
      builtinProviders: ["future-provider"],
      builtinModels: () => [{ api: "openai-responses" }],
      oauthProviders: [],
      descriptions: DESCRIPTIONS,
      exposedApis: new Set(["openai-responses"]),
    }),
    /has no safe description/u,
  );
  assert.throws(
    () => discoverSupportedProviders({
      builtinProviders: ["openai"],
      builtinModels: () => [{ api: "openai-responses" }],
      oauthProviders: [],
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
    builtinProviders: ["openai", "google"],
    builtinModels: (provider) => [{
      api: provider === "openai" ? "openai-responses" : "google-generative-ai",
    }],
    oauthProviders: [],
    descriptions: { openai: "OpenAI", google: "Google" },
    exposedApis: new Set(["openai-responses"]),
  });
  assert.deepEqual(providers.map(({ provider }) => provider), ["openai"]);
});
