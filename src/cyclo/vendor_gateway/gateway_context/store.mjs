// Shared helpers for the gateway's private credential store. Used by both the
// server (refresh write-back) and login.mjs (provisioning). Gateway processes
// are the only writers; this cross-process lock serializes containers sharing a
// volume, and atomic rename keeps every read consistent.

import { readFileSync, writeFileSync, renameSync, openSync, closeSync, unlinkSync, mkdirSync, statSync } from "node:fs";
import { dirname } from "node:path";

const LOCK_TIMEOUT_MS = 10_000;
// A lock older than this is treated as orphaned (holder crashed / was SIGKILLed
// on container stop) and stolen. Must stay well above any legitimate hold time
// (a JSON read + one OAuth refresh, seconds) so a slow-but-live holder is never
// robbed; the atomic openSync("wx") still arbitrates who wins after a steal.
const STALE_LOCK_MS = 30_000;

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
export async function withFileLock(path, fn) {
  const lockPath = path + ".lock";
  mkdirSync(dirname(path), { recursive: true });
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  let fd;
  for (;;) {
    try {
      fd = openSync(lockPath, "wx");
      break;
    } catch (exc) {
      if (exc.code !== "EEXIST") throw exc;
      try {
        if (Date.now() - statSync(lockPath).mtimeMs > STALE_LOCK_MS) {
          unlinkSync(lockPath); // break a lock orphaned by a crashed holder
          continue;
        }
      } catch (statExc) {
        if (statExc.code === "ENOENT") continue; // released under us; retry now
      }
      if (Date.now() > deadline) throw new Error(`timed out acquiring lock: ${lockPath}`);
      await new Promise((r) => setTimeout(r, 25));
    }
  }
  try {
    writeFileSync(fd, `${process.pid}:${Date.now()}\n`); // diagnostic only
  } catch {
    /* content is best-effort; staleness uses mtime */
  }
  try {
    return await fn();
  } finally {
    closeSync(fd);
    try {
      unlinkSync(lockPath);
    } catch {
      /* already gone */
    }
  }
}

export function writeJsonAtomic(path, data) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = path + ".tmp";
  writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", { mode: 0o600 });
  renameSync(tmp, path);
}
