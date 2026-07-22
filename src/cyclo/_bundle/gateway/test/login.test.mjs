import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
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

test("login syntax is strict and supports multiple accounts", () => {
  assert.deepEqual(parseLoginArgs(["anthropic", "--as", "claude-work"]), {
    provider: "anthropic",
    account: "claude-work",
    apiKeyEnv: undefined,
    apiKeyStdin: false,
  });
  assert.throws(() => parseLoginArgs(["../escape"]), /provider name/u);
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

test("OAuth login delegates to the selected Pi provider and stores its credential", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-gateway-oauth-login-"));
  const path = join(directory, "auth.json");
  const output = new PassThrough();
  let text = "";
  output.setEncoding("utf8");
  output.on("data", (chunk) => { text += chunk; });
  const provider = {
    id: "openai-codex",
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
  };
  try {
    await login(["openai-codex", "--as", "work"], {
      env: { CYCLO_GATEWAY_AUTH_JSON: path },
      output,
      providers: [provider],
      getProvider: (id) => id === provider.id ? provider : undefined,
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
