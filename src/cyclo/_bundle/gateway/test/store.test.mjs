import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { readJson, withFileLock, writeJsonAtomic } from "../src/store.mjs";

test("the JSON store writes atomically with private permissions", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-store-"));
  t.after(async () => (await import("node:fs/promises")).rm(root, { recursive: true, force: true }));
  const path = join(root, "state", "auth.json");

  assert.equal(readJson(path), null);
  writeJsonAtomic(path, { account: { type: "api_key", key: "secret" } });
  assert.deepEqual(readJson(path), { account: { type: "api_key", key: "secret" } });
  assert.equal((await stat(path)).mode & 0o777, 0o600);
  assert.match(await readFile(path, "utf8"), /\n$/u);
});

test("the kernel lock excludes another writer until release", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-lock-"));
  t.after(async () => (await import("node:fs/promises")).rm(root, { recursive: true, force: true }));
  const path = join(root, "auth.json");
  const order = [];
  let release;
  let entered;
  const firstEntered = new Promise((resolve) => { entered = resolve; });
  const gate = new Promise((resolve) => { release = resolve; });

  const first = withFileLock(path, async () => {
    order.push("first-enter");
    entered();
    await gate;
    order.push("first-exit");
  });
  await firstEntered;
  const second = withFileLock(path, async () => order.push("second-enter"));
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(order, ["first-enter"]);
  release();
  await Promise.all([first, second]);
  assert.deepEqual(order, ["first-enter", "first-exit", "second-enter"]);
  assert.equal((await stat(`${path}.lock`)).mode & 0o777, 0o600);
});
