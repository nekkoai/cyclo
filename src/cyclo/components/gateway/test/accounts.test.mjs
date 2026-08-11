import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { logout, rename } from "../src/accounts.mjs";

test("logout removes only the selected credential and leaves usage untouched", async (t) => {
  const root = await fixture(t);
  const authPath = join(root, "auth.json");
  const usagePath = join(root, "usage.jsonl");
  const original = {
    work: { type: "api_key", key: "work-secret", provider: "openai" },
    personal: {
      type: "oauth",
      access: "access-secret",
      refresh: "refresh-secret",
      expires: 2_000_000_000_000,
      provider: "openai-codex",
    },
  };
  const usage = "{\"model\":\"work/gpt-test\"}\n";
  await writeFile(authPath, `${JSON.stringify(original)}\n`, { mode: 0o600 });
  await writeFile(usagePath, usage, { mode: 0o600 });
  const output = capture();

  await logout(["work"], {
    env: { CYCLO_GATEWAY_AUTH_JSON: authPath },
    output,
  });

  assert.deepEqual(JSON.parse(await readFile(authPath, "utf8")), {
    personal: original.personal,
  });
  assert.equal(await readFile(usagePath, "utf8"), usage);
  assert.equal((await stat(authPath)).mode & 0o777, 0o600);
  assert.equal(output.text, "removed stored credential for work\n");
  assert.doesNotMatch(output.text, /work-secret|access-secret|refresh-secret/u);
});

test("rename moves one account without changing its provider or usage", async (t) => {
  const root = await fixture(t);
  const authPath = join(root, "auth.json");
  const usagePath = join(root, "usage.jsonl");
  const original = {
    old_name: {
      type: "oauth",
      access: "access-secret",
      refresh: "refresh-secret",
      expires: 2_000_000_000_000,
      provider: "openai-codex",
    },
    other: { type: "api_key", key: "other-secret", provider: "anthropic" },
  };
  const usage = "past usage remains attributable to old_name/gpt-test\n";
  await writeFile(authPath, `${JSON.stringify(original)}\n`, { mode: 0o600 });
  await writeFile(usagePath, usage, { mode: 0o600 });
  const output = capture();

  await rename(["old_name", "new-name"], {
    env: { CYCLO_GATEWAY_AUTH_JSON: authPath },
    output,
  });

  const stored = JSON.parse(await readFile(authPath, "utf8"));
  assert.equal(Object.hasOwn(stored, "old_name"), false);
  assert.deepEqual(stored["new-name"], original.old_name);
  assert.deepEqual(stored.other, original.other);
  assert.equal(await readFile(usagePath, "utf8"), usage);
  assert.equal(output.text, "renamed stored credential old_name to new-name\n");
  assert.doesNotMatch(output.text, /access-secret|refresh-secret|other-secret/u);
});

test("rename preserves the inferred provider identity of a legacy credential", async (t) => {
  const root = await fixture(t);
  const authPath = join(root, "auth.json");
  await writeFile(authPath, JSON.stringify({
    openai: { type: "api_key", key: "private-key" },
  }), { mode: 0o600 });

  await rename(["openai", "work"], {
    env: { CYCLO_GATEWAY_AUTH_JSON: authPath },
    output: capture(),
  });

  assert.deepEqual(JSON.parse(await readFile(authPath, "utf8")), {
    work: { type: "api_key", key: "private-key", provider: "openai" },
  });
});

test("failed account mutations leave the credential file byte-for-byte unchanged", async (t) => {
  const root = await fixture(t);
  const authPath = join(root, "auth.json");
  const original = `${JSON.stringify({
    work: { type: "api_key", key: "private-key", provider: "openai" },
    existing: { type: "api_key", key: "other-key", provider: "anthropic" },
  }, null, 4)}\n`;
  const options = {
    env: { CYCLO_GATEWAY_AUTH_JSON: authPath },
    output: capture(),
  };
  const failures = [
    [() => logout(["missing"], options), /account missing is not stored/u],
    [() => rename(["missing", "new"], options), /account missing is not stored/u],
    [() => rename(["work", "existing"], options), /account existing is already stored/u],
    [() => rename(["work", "work"], options), /must differ/u],
  ];

  for (const [operation, pattern] of failures) {
    await writeFile(authPath, original, { mode: 0o600 });
    await assert.rejects(operation(), pattern);
    assert.equal(await readFile(authPath, "utf8"), original);
  }
});

test("account command syntax and names are strict", async () => {
  const options = {
    env: { CYCLO_GATEWAY_AUTH_JSON: "/unused/auth.json" },
    output: capture(),
  };
  await assert.rejects(logout([], options), /usage: logout ACCOUNT/u);
  await assert.rejects(logout(["work", "extra"], options), /usage: logout ACCOUNT/u);
  await assert.rejects(rename(["work"], options), /usage: rename OLD_ACCOUNT NEW_ACCOUNT/u);
  await assert.rejects(rename(["work", "new", "extra"], options), /usage: rename/u);
  await assert.rejects(logout(["../escape"], options), /account name/u);
  await assert.rejects(rename(["work", "_hidden"], options), /target account name/u);
});

test("account mutations reject a malformed store without replacing it", async (t) => {
  const root = await fixture(t);
  const authPath = join(root, "auth.json");
  const original = "[]\n";
  await writeFile(authPath, original, { mode: 0o600 });

  await assert.rejects(logout(["work"], {
    env: { CYCLO_GATEWAY_AUTH_JSON: authPath },
    output: capture(),
  }), /credential store must be a JSON object/u);
  assert.equal(await readFile(authPath, "utf8"), original);
});

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-accounts-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

function capture() {
  return {
    text: "",
    write(value) {
      this.text += String(value);
    },
  };
}
