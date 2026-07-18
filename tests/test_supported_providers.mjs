import { test } from "node:test";
import assert from "node:assert/strict";

import {
  formatSupportedProviders,
} from "../src/cyclo/credential_gateway/gateway_context/supported-providers.mjs";


test("supported provider table gives copyable login commands", () => {
  const output = formatSupportedProviders(
    ["openai", "openai-codex", "anthropic", "github-copilot", "google"],
    ["github-copilot", "anthropic", "openai-codex", "not-a-builtin"],
  );

  assert.equal(
    output,
    [
      "PROVIDER\tDESCRIPTION\tAUTH\tLOGIN",
      "anthropic\tAnthropic Claude through a Claude Pro/Max subscription\toauth\tcyclo gateway login anthropic",
      "github-copilot\tGitHub Copilot subscription\toauth\tcyclo gateway login github-copilot",
      "google\tGoogle Gemini Developer API\tapi-key\tcyclo gateway login google --api-key-stdin",
      "openai\tOpenAI API for GPT and o-series models\tapi-key\tcyclo gateway login openai --api-key-stdin",
      "openai-codex\tOpenAI Codex through a ChatGPT Plus/Pro subscription\toauth\tcyclo gateway login openai-codex",
      "",
      "Account/catalogue names default to PROVIDER. Add --as NAME to choose one; NAME uses lowercase letters, numbers, underscore, or hyphen.",
    ].join("\n"),
  );
});


test("supported provider formatter rejects unsafe or ambiguous registries", () => {
  for (const providers of [
    [],
    ["openai", "openai"],
    ["bad.provider"],
    ["bad\tprovider"],
    ["bad\nprovider"],
    ["\u001b[31mbad"],
    [42],
  ]) {
    assert.throws(
      () => formatSupportedProviders(providers, []),
      /invalid built-in provider registry/,
    );
  }

  for (const oauthProviders of [
    ["anthropic", "anthropic"],
    ["bad\tprovider"],
    ["\u001b[31mbad"],
    [42],
  ]) {
    assert.throws(
      () => formatSupportedProviders(["anthropic"], oauthProviders),
      /invalid OAuth provider registry/,
    );
  }

  assert.throws(
    () => formatSupportedProviders(["future-provider"], [], {}),
    /has no safe description/,
  );
  assert.throws(
    () =>
      formatSupportedProviders(["openai"], [], {
        openai: "unsafe\tdescription",
      }),
    /has no safe description/,
  );
});


test(
  "supported provider formatter permits no OAuth providers and preserves input",
  () => {
    const providers = ["zai", "openai"];
    const oauthProviders = [];

    assert.equal(
    formatSupportedProviders(providers, oauthProviders),
    [
      "PROVIDER\tDESCRIPTION\tAUTH\tLOGIN",
      "openai\tOpenAI API for GPT and o-series models\tapi-key\tcyclo gateway login openai --api-key-stdin",
      "zai\tZ.AI international coding API for GLM models\tapi-key\tcyclo gateway login zai --api-key-stdin",
      "",
      "Account/catalogue names default to PROVIDER. Add --as NAME to choose one; NAME uses lowercase letters, numbers, underscore, or hyphen.",
      ].join("\n"),
    );
    assert.deepEqual(providers, ["zai", "openai"]);
    assert.deepEqual(oauthProviders, []);
  },
);
