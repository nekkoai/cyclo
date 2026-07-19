import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { withFileLock } from "../src/cyclo/credential_gateway/gateway_context/store.mjs";

const STORE_MODULE = new URL(
  "../src/cyclo/credential_gateway/gateway_context/store.mjs",
  import.meta.url,
).href;

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function waitForMarker(child, marker, timeoutMs = 2_000) {
  return new Promise((resolve, reject) => {
    let poll;
    let timeout;
    const cleanup = () => {
      clearInterval(poll);
      clearTimeout(timeout);
      child.off("error", onError);
      child.off("exit", onExit);
    };
    const finish = (callback, value) => {
      cleanup();
      callback(value);
    };
    const onError = (error) => finish(reject, error);
    const onExit = (code, signal) => finish(
      reject,
      new Error(
        "lock-holder process exited before acquiring the lock: "
          + (signal ? `signal ${signal}` : `status ${code}`),
      ),
    );
    const check = () => {
      if (existsSync(marker)) finish(resolve);
    };
    child.once("error", onError);
    child.once("exit", onExit);
    poll = setInterval(check, 10);
    timeout = setTimeout(
      () => finish(reject, new Error("timed out waiting for lock-holder marker")),
      timeoutMs,
    );
    check();
  });
}

test("a normal contender cannot enter before the holder releases", async () => {
  const root = mkdtempSync(join(tmpdir(), "cyclo-gateway-store-serial-"));
  const store = join(root, "auth.json");
  const enteredA = deferred();
  const releaseA = deferred();
  const enteredB = deferred();
  const releaseB = deferred();
  let holderA;
  let holderB;

  try {
    holderA = withFileLock(store, async () => {
      enteredA.resolve();
      await releaseA.promise;
    });
    await enteredA.promise;

    let bEntered = false;
    holderB = withFileLock(
      store,
      async () => {
        bEntered = true;
        enteredB.resolve();
        await releaseB.promise;
      },
      { timeoutMs: 2_000 },
    );
    await delay(100);
    assert.equal(bEntered, false);

    releaseA.resolve();
    await holderA;
    await enteredB.promise;
    releaseB.resolve();
    await holderB;
  } finally {
    releaseA.resolve();
    releaseB.resolve();
    await Promise.allSettled([holderA, holderB].filter(Boolean));
    rmSync(root, { recursive: true, force: true });
  }
});

test("normal release preserves the private lock pathname and inode", async () => {
  const root = mkdtempSync(join(tmpdir(), "cyclo-gateway-store-lock-"));
  const store = join(root, "auth.json");
  const lockPath = `${store}.lock`;
  const enteredA = deferred();
  const releaseA = deferred();
  let holderA;

  try {
    holderA = withFileLock(store, async () => {
      enteredA.resolve();
      await releaseA.promise;
    });
    await enteredA.promise;
    const inode = statSync(lockPath).ino;

    releaseA.resolve();
    await holderA;
    assert.equal(statSync(lockPath).ino, inode);
    await withFileLock(store, async () => {
      assert.equal(statSync(lockPath).ino, inode);
    });
    assert.equal(statSync(lockPath).ino, inode);
  } finally {
    releaseA.resolve();
    await Promise.allSettled([holderA].filter(Boolean));
    rmSync(root, { recursive: true, force: true });
  }
});

test("SIGKILL of a holder process releases the kernel lock", async () => {
  const root = mkdtempSync(join(tmpdir(), "cyclo-gateway-store-crash-"));
  const store = join(root, "auth.json");
  const locked = join(root, "holder.locked");
  const source = `
    import { writeFileSync } from "node:fs";
    import { withFileLock } from ${JSON.stringify(STORE_MODULE)};
    await withFileLock(process.env.CYCLO_TEST_STORE, async () => {
      writeFileSync(process.env.CYCLO_TEST_LOCKED, "locked\\n", {
        flag: "wx",
        mode: 0o600,
      });
      await new Promise(() => setInterval(() => {}, 1000));
    });
  `;
  const holder = spawn(process.execPath, ["--input-type=module", "--eval", source], {
    env: {
      CYCLO_TEST_STORE: store,
      CYCLO_TEST_LOCKED: locked,
    },
    stdio: ["ignore", "ignore", "inherit"],
  });
  const holderExited = new Promise((resolve) => {
    holder.once("exit", (code, signal) => resolve({ code, signal }));
  });
  const entered = deferred();
  const release = deferred();
  let contender;

  try {
    await waitForMarker(holder, locked);
    let contenderEntered = false;
    contender = withFileLock(
      store,
      async () => {
        contenderEntered = true;
        entered.resolve();
        await release.promise;
      },
      { timeoutMs: 2_000 },
    );
    await delay(100);
    assert.equal(contenderEntered, false);

    assert.equal(holder.kill("SIGKILL"), true);
    const exit = await holderExited;
    assert.equal(exit.signal, "SIGKILL");
    // If the lock is not released, the contender rejects after its bounded
    // acquisition timeout. Race that rejection against entry so the test
    // reports the failure instead of waiting forever on an unresolved marker.
    await Promise.race([entered.promise, contender]);
    release.resolve();
    await contender;
  } finally {
    holder.kill("SIGKILL");
    release.resolve();
    await Promise.allSettled([holderExited, contender].filter(Boolean));
    rmSync(root, { recursive: true, force: true });
  }
});
