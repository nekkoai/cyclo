import assert from "node:assert/strict";
import fs from "node:fs";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
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
  await assert.rejects(stat(`${path}.tmp`), { code: "ENOENT" });
});

test("the JSON store makes the replacement durable in commit order", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-durable-store-"));
  t.after(async () => (await import("node:fs/promises")).rm(root, { recursive: true, force: true }));
  const path = join(root, "auth.json");
  const events = [];
  const originalFsync = fs.fsyncSync;
  const originalRename = fs.renameSync;
  const originalWrite = fs.writeFileSync;
  const restore = replaceBuiltinFs({
    fsyncSync(descriptor) {
      events.push("fsync:directory");
      return originalFsync(descriptor);
    },
    renameSync(source, destination) {
      events.push("rename");
      return originalRename(source, destination);
    },
    writeFileSync(destination, data, options) {
      assert.equal(options.flush, true);
      events.push("write:flush");
      return originalWrite(destination, data, options);
    },
  });

  try {
    writeJsonAtomic(path, { value: "new" });
  } finally {
    restore();
  }

  assert.deepEqual(events, ["write:flush", "rename", "fsync:directory"]);
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

function replaceBuiltinFs(replacements) {
  const originals = Object.fromEntries(
    Object.keys(replacements).map((name) => [name, fs[name]]),
  );
  Object.assign(fs, replacements);
  syncBuiltinESMExports();
  return () => {
    Object.assign(fs, originals);
    syncBuiltinESMExports();
  };
}
