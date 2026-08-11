import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";

import { login, parseLoginArgs } from "../src/login.mjs";
import { createAuthInteraction } from "../src/oauth-ui.mjs";

test("API-key login writes the compatible private store without exposing the key", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-login-"));
  const path = join(directory, "auth.json");
  const output = new PassThrough();
  let text = "";
  output.setEncoding("utf8");
  output.on("data", (chunk) => { text += chunk; });
  try {
    await login(["openai", "--as", "work", "--api-key-env", "TEST_KEY"], {
      env: { CYCLO_GATEWAY_AUTH_JSON: path, TEST_KEY: "private-key" },
      output,
    });
    const stored = JSON.parse(await readFile(path, "utf8"));
    assert.deepEqual(stored, {
      work: { type: "api_key", key: "private-key", provider: "openai" },
    });
    assert.match(text, /stored api_key credential for work/u);
    assert.doesNotMatch(text, /private-key/u);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("an unknown provider cannot replace a working credential store", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-login-transaction-"));
  const path = join(directory, "auth.json");
  const original = {
    work: { type: "api_key", key: "old-private-key", provider: "known" },
  };
  const provider = fakeProvider("known");
  try {
    await writeFile(path, `${JSON.stringify(original)}\n`);
    await assert.rejects(login(
      ["unknown", "--api-key-env", "TEST_KEY"],
      {
        env: { CYCLO_GATEWAY_AUTH_JSON: path, TEST_KEY: "new-private-key" },
        output: new PassThrough(),
        providers: [provider],
        getProvider: (id) => id === provider.id ? provider : undefined,
        getApiProvider: apiProvider,
      },
    ), /unknown provider/u);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), original);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("candidate validation cannot mutate the credential that is committed", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-login-isolation-"));
  const path = join(directory, "auth.json");
  try {
    await login(["openai", "--api-key-env", "TEST_KEY"], {
      env: { CYCLO_GATEWAY_AUTH_JSON: path, TEST_KEY: "private-key" },
      output: new PassThrough(),
      validateStore(candidate) {
        candidate.openai.key = "validator-mutation";
      },
    });
    assert.equal(
      JSON.parse(await readFile(path, "utf8")).openai.key,
      "private-key",
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("custom providers validate before commit and remain supported", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-login-custom-"));
  const path = join(directory, "auth.json");
  const modelsPath = join(directory, "models.json");
  const provider = fakeProvider("known");
  try {
    await writeFile(modelsPath, JSON.stringify({
      providers: {
        custom: {
          api: "openai-responses",
          baseUrl: "https://custom.invalid/v1",
          models: [{
            id: "usable",
            input: ["text"],
            contextWindow: 4096,
            maxTokens: 1024,
          }],
        },
      },
    }));
    await login(["custom", "--api-key-env", "TEST_KEY"], {
      env: {
        CYCLO_GATEWAY_AUTH_JSON: path,
        CYCLO_GATEWAY_MODELS_JSON: modelsPath,
        TEST_KEY: "private-key",
      },
      output: new PassThrough(),
      providers: [provider],
      getProvider: () => undefined,
      getApiProvider: apiProvider,
    });
    assert.equal(
      JSON.parse(await readFile(path, "utf8")).custom.provider,
      "custom",
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("a custom provider with no usable models is not committed", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-login-invalid-"));
  const path = join(directory, "auth.json");
  const modelsPath = join(directory, "models.json");
  const original = {
    work: { type: "api_key", key: "old-private-key", provider: "known" },
  };
  const provider = fakeProvider("known");
  try {
    await writeFile(path, `${JSON.stringify(original)}\n`);
    await writeFile(modelsPath, JSON.stringify({
      providers: {
        broken: {
          api: "openai-responses",
          baseUrl: "https://broken.invalid/v1",
          models: [{
            id: "missing-output-limit",
            input: ["text"],
            contextWindow: 4096,
          }],
        },
      },
    }));
    await assert.rejects(login(
      ["broken", "--api-key-env", "TEST_KEY"],
      {
        env: {
          CYCLO_GATEWAY_AUTH_JSON: path,
          CYCLO_GATEWAY_MODELS_JSON: modelsPath,
          TEST_KEY: "new-private-key",
        },
        output: new PassThrough(),
        providers: [provider],
        getProvider: () => undefined,
        getApiProvider: apiProvider,
      },
    ), /exposes no usable models/u);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), original);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("login syntax is strict and supports multiple accounts", () => {
  assert.deepEqual(parseLoginArgs(["anthropic", "--as", "claude-work"]), {
    provider: "anthropic",
    account: "claude-work",
    apiKeyEnv: undefined,
    apiKeyStdin: false,
  });
  assert.throws(() => parseLoginArgs(["../escape"]), /provider name/u);
  assert.throws(() => parseLoginArgs(["_legacy"]), /provider name/u);
  assert.throws(() => parseLoginArgs(["-legacy"]), /provider name/u);
  assert.throws(() => parseLoginArgs(["a".repeat(65)]), /provider name/u);
  assert.throws(
    () => parseLoginArgs(["openai", "--as", "_legacy"]),
    /account name/u,
  );
  assert.deepEqual(parseLoginArgs(["a".repeat(64), "--as", "work_1"]), {
    provider: "a".repeat(64),
    account: "work_1",
    apiKeyEnv: undefined,
    apiKeyStdin: false,
  });
  assert.throws(() => parseLoginArgs(["openai", "--wat"]), /unknown argument/u);
  assert.throws(
    () => parseLoginArgs(["openai", "--api-key", "must-not-enter-argv"]),
    /unknown argument/u,
  );
});

test("OAuth interaction implements the pi-ai selection prompt", async () => {
  const interaction = createAuthInteraction({
    ask: async () => "2",
    write() {},
  });
  assert.equal(interaction.signal.aborted, false);
  assert.equal(typeof interaction.prompt, "function");
  assert.equal(await interaction.prompt({
    type: "select",
    message: "Choose account",
    options: [
      { id: "one", label: "One" },
      { id: "two", label: "Two" },
    ],
  }), "two");
});

test("OAuth login supplies the provider-level cancellation signal", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-oauth-signal-"));
  const path = join(directory, "auth.json");
  const provider = {
    ...fakeProvider("openai-codex"),
    auth: {
      oauth: {
        async login(interaction) {
          // OpenAI device-code polling reads this immediately after displaying
          // the one-time code.  This reproduces the real provider contract.
          assert.equal(interaction.signal.aborted, false);
          interaction.notify({
            type: "device_code",
            verificationUri: "https://auth.openai.com/codex/device",
            userCode: "TEST-CODE",
          });
          return {
            access: "oauth-access",
            refresh: "oauth-refresh",
            expires: Date.now() + 3_600_000,
          };
        },
      },
    },
  };
  try {
    await login(["openai-codex", "--as", "work"], {
      env: { CYCLO_GATEWAY_AUTH_JSON: path },
      output: new PassThrough(),
      providers: [provider],
      getProvider: (id) => id === provider.id ? provider : undefined,
      getApiProvider: apiProvider,
    });
    assert.equal(JSON.parse(await readFile(path, "utf8")).work.type, "oauth");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("OAuth interaction stops before Pi dispatch when selection is cancelled", async () => {
  const interaction = createAuthInteraction({
    ask: async () => "q",
    write() {},
  });
  await assert.rejects(interaction.prompt({
    type: "select",
    message: "Choose account",
    options: [{ id: "one", label: "One" }],
  }), /Login cancelled/u);
});

test("OAuth login delegates to the selected Pi provider and stores its credential", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-oauth-login-"));
  const path = join(directory, "auth.json");
  const output = new PassThrough();
  let text = "";
  output.setEncoding("utf8");
  output.on("data", (chunk) => { text += chunk; });
  const provider = {
    id: "openai-codex",
    baseUrl: "https://oauth.invalid/v1",
    auth: {
      oauth: {
        async login(interaction) {
          interaction.notify({ type: "progress", message: "Authorizing" });
          return {
            access: "oauth-access",
            refresh: "oauth-refresh",
            expires: Date.now() + 3_600_000,
          };
        },
      },
    },
    getModels() {
      return [{
        id: "model",
        api: "openai-responses",
        input: ["text"],
        contextWindow: 4096,
        maxTokens: 1024,
      }];
    },
    streamSimple() {},
  };
  try {
    await login(["openai-codex", "--as", "work"], {
      env: { CYCLO_GATEWAY_AUTH_JSON: path },
      output,
      providers: [provider],
      getProvider: (id) => id === provider.id ? provider : undefined,
      getApiProvider: apiProvider,
    });
    const stored = JSON.parse(await readFile(path, "utf8"));
    assert.equal(stored.work.type, "oauth");
    assert.equal(stored.work.provider, "openai-codex");
    assert.equal(stored.work.access, "oauth-access");
    assert.match(text, /Authorizing/u);
    assert.doesNotMatch(text, /oauth-access|oauth-refresh/u);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

function fakeProvider(id) {
  return {
    id,
    baseUrl: "https://provider.invalid/v1",
    auth: { apiKey: {} },
    getModels() {
      return [{
        id: "model",
        api: "openai-responses",
        input: ["text"],
        contextWindow: 4096,
        maxTokens: 1024,
      }];
    },
    streamSimple() {},
  };
}

function apiProvider(api) {
  if (api !== "openai-responses") return undefined;
  return { api, stream() {}, streamSimple() {} };
}
