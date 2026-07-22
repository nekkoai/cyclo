import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createCredentialResolver } from "../src/credentials.mjs";

function providerWithOAuth(oauth) {
  return {
    auth: {
      oauth: {
        login() {},
        ...oauth,
      },
    },
  };
}

async function credentialFixture(t, store) {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-credentials-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const authPath = join(root, "auth.json");
  await writeFile(authPath, `${JSON.stringify(store)}\n`, { mode: 0o600 });
  return authPath;
}

test("API keys are read dynamically and legacy records remain compatible", async (t) => {
  const authPath = await credentialFixture(t, {
    openai: { type: "api_key", key: "first-secret" },
  });
  const resolver = createCredentialResolver({ authPath, getProvider: () => undefined });
  const route = { account: "openai", provider: "openai" };
  const first = await resolver.resolve(route);
  assert.deepEqual(first, { apiKey: "first-secret" });

  await writeFile(authPath, JSON.stringify({
    openai: { type: "api_key", key: "second-secret" },
  }));
  const second = await resolver.resolve(route);
  assert.deepEqual(second, { apiKey: "second-secret" });
  assert.equal(Object.isFrozen(second), true);
});

test("separate resolvers serialize OAuth refresh and reuse the winner", async (t) => {
  const authPath = await credentialFixture(t, {
    work: {
      type: "oauth",
      provider: "openai-codex",
      access: "expired-access",
      refresh: "refresh-secret",
      expires: 1,
      retained: "value",
    },
  });
  let refreshes = 0;
  const oauth = {
    async refresh() {
      refreshes += 1;
      await new Promise((resolve) => setTimeout(resolve, 50));
      return {
        access: "fresh-access",
        refresh: "fresh-refresh",
        expires: Date.now() + 3_600_000,
      };
    },
    toAuth(credential) {
      return { apiKey: `api:${credential.access}` };
    },
  };
  const make = () => createCredentialResolver({
    authPath,
    getProvider: (provider) => provider === "openai-codex"
      ? providerWithOAuth(oauth)
      : undefined,
  });
  const route = { account: "work", provider: "openai-codex" };
  const [left, right] = await Promise.all([make().resolve(route), make().resolve(route)]);

  assert.equal(refreshes, 1);
  assert.equal(left.apiKey, "api:fresh-access");
  assert.equal(right.apiKey, "api:fresh-access");
  const stored = JSON.parse(await readFile(authPath, "utf8"));
  assert.equal(stored.work.retained, "value");
  assert.equal(stored.work.provider, "openai-codex");
  assert.equal(stored.work.access, "fresh-access");
});

test("a route cannot consume an account that changed provider", async (t) => {
  const authPath = await credentialFixture(t, {
    work: { type: "api_key", provider: "anthropic", key: "secret" },
  });
  const { resolve } = createCredentialResolver({ authPath, getProvider: () => undefined });
  await assert.rejects(
    resolve({ account: "work", provider: "openai" }),
    /no longer belongs/u,
  );
});

test("partial OAuth records fail health checks and resolution", async (t) => {
  const authPath = await credentialFixture(t, {
    work: {
      type: "oauth",
      provider: "openai-codex",
      access: "access-without-refresh-or-expiry",
    },
  });
  const oauth = { refresh() {}, toAuth() {} };
  const resolver = createCredentialResolver({
    authPath,
    getProvider: () => providerWithOAuth(oauth),
  });
  const route = { account: "work", provider: "openai-codex" };
  assert.throws(() => resolver.check([route]), /complete OAuth credential/u);
  await assert.rejects(resolver.resolve(route), /complete OAuth credential/u);
});

test("OAuth may derive an account-specific native model route", async (t) => {
  const authPath = await credentialFixture(t, {
    copilot: {
      type: "oauth",
      provider: "github-copilot",
      access: "fresh-access",
      refresh: "refresh-secret",
      expires: Date.now() + 3_600_000,
    },
  });
  const oauth = {
    refresh() {},
    toAuth: ({ access }) => ({
      apiKey: access,
      baseUrl: "https://api.account.example/v1",
      headers: { "x-account": "copilot" },
    }),
  };
  const resolver = createCredentialResolver({
    authPath,
    getProvider: () => providerWithOAuth(oauth),
  });
  const resolved = await resolver.resolve({
    account: "copilot",
    provider: "github-copilot",
    rawModel: {
      id: "model",
      provider: "github-copilot",
      api: "openai-responses",
      baseUrl: "https://default.example/v1",
    },
  });
  assert.equal(resolved.effectiveModel.baseUrl, "https://api.account.example/v1");
  assert.equal(resolved.effectiveModel.headers["x-account"], "copilot");
  assert.equal(resolved.apiKey, "fresh-access");
});
