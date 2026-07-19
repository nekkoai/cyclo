// Shared helpers for the gateway's private credential store. Used by both the
// server (refresh write-back) and login.mjs (provisioning). Gateway processes
// are the only writers; a kernel flock serializes containers sharing a volume,
// and atomic rename keeps every read consistent.

import { spawn } from "node:child_process";
import {
  closeSync,
  constants as fsConstants,
  fchmodSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname } from "node:path";

const LOCK_TIMEOUT_MS = 10_000;
// Never unlink a lock pathname to recover a presumed-stale owner. That can
// create a replacement inode while the old owner still holds the first. flock
// is instead released by the kernel when the owning process closes its file
// descriptor or exits.
const LOCK_TIMEOUT_EXIT = 75;

export function readJson(path) {
  let text;
  try {
    text = readFileSync(path, "utf8");
  } catch (exc) {
    if (exc?.code === "ENOENT") return null;
    throw new Error(`failed to read JSON file ${path}: ${exc.message}`, { cause: exc });
  }
  try {
    return JSON.parse(text);
  } catch (exc) {
    throw new Error(`invalid JSON file ${path}: ${exc.message}`, { cause: exc });
  }
}
function acquireFileLock(lockPath, timeoutMs) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error("lock timeout must be a positive number");
  }
  const waitSeconds = Math.max(0.001, timeoutMs / 1000).toFixed(3);
  let descriptor;
  try {
    descriptor = openSync(
      lockPath,
      fsConstants.O_RDWR
        | fsConstants.O_CREAT
        | fsConstants.O_NOFOLLOW,
      0o600,
    );
    fchmodSync(descriptor, 0o600);
  } catch (error) {
    if (descriptor !== undefined) closeSync(descriptor);
    throw new Error(
      `failed to prepare lock file ${lockPath}: ${error.message}`,
      { cause: error },
    );
  }

  // fd 3 is duped from the descriptor above and therefore refers to the same
  // kernel open-file description. flock applies the lock and exits; this Node
  // process retains the lock through its original descriptor without keeping a
  // helper alive. Closing the descriptor (including on process death) releases
  // the lock, and can never affect a replacement pathname/inode.
  let child;
  try {
    child = spawn(
      "/usr/bin/flock",
      [
        "--exclusive",
        "--wait",
        waitSeconds,
        "--conflict-exit-code",
        String(LOCK_TIMEOUT_EXIT),
        "3",
      ],
      { stdio: ["ignore", "ignore", "pipe", descriptor] },
    );
  } catch (error) {
    closeSync(descriptor);
    throw new Error(
      `failed to execute /usr/bin/flock for ${lockPath}: ${error.message}`,
      { cause: error },
    );
  }
  let stderr = "";
  let settled = false;
  return new Promise((resolve, reject) => {
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      if (stderr.length < 4096) stderr += chunk;
    });
    child.once("error", (error) => {
      if (settled) return;
      settled = true;
      closeSync(descriptor);
      reject(
        new Error(
          `failed to execute /usr/bin/flock for ${lockPath}: ${error.message}`,
          { cause: error },
        ),
      );
    });
    child.once("exit", (code, signal) => {
      if (settled) return;
      settled = true;
      if (code === 0) {
        let held = true;
        resolve({
          release() {
            if (!held) return;
            held = false;
            closeSync(descriptor);
          },
        });
        return;
      }
      closeSync(descriptor);
      if (code === LOCK_TIMEOUT_EXIT) {
        reject(new Error(`timed out acquiring lock: ${lockPath}`));
        return;
      }
      const detail = stderr.trim()
        || (signal ? `signal ${signal}` : `exit status ${code}`);
      reject(new Error(`failed to acquire lock ${lockPath}: ${detail}`));
    });
  });
}

export async function withFileLock(path, fn, { timeoutMs = LOCK_TIMEOUT_MS } = {}) {
  const lockPath = path + ".lock";
  mkdirSync(dirname(path), { recursive: true });
  const lock = await acquireFileLock(lockPath, timeoutMs);
  let callbackFailed = false;
  let callbackError;
  let result;
  try {
    result = await fn();
  } catch (error) {
    callbackFailed = true;
    callbackError = error;
  }
  try {
    lock.release();
  } catch (releaseError) {
    if (!callbackFailed) throw releaseError;
  }
  if (callbackFailed) throw callbackError;
  return result;
}

export function writeJsonAtomic(path, data) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = path + ".tmp";
  writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", { mode: 0o600 });
  renameSync(tmp, path);
}
