// Byte-exact response scrubbing at the credential boundary. The redactor keeps
// only the suffix that could still become a secret on the next push, so memory
// retained between upstream chunks is bounded by the longest secret.

const MARKER_BYTES = Buffer.from("[REDACTED]", "ascii");
const EMPTY = Buffer.alloc(0);
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/u;

// Export a copy: callers can inspect it, but mutating it cannot change the
// marker used by the redactor.
export const REDACTION_MARKER = Buffer.from(MARKER_BYTES);


function copyBytes(value, label, allowString = true) {
  if (allowString && typeof value === "string") return Buffer.from(value, "utf8");
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    return Buffer.from(value);
  }
  throw new TypeError(`${label} must be a string, Buffer, or Uint8Array`);
}


export function normalizeResponseSecrets(secrets) {
  if (
    secrets === null
    || secrets === undefined
    || typeof secrets === "string"
    || Buffer.isBuffer(secrets)
    || secrets instanceof Uint8Array
    || typeof secrets[Symbol.iterator] !== "function"
  ) {
    throw new TypeError("response secrets must be an iterable of byte values");
  }

  const unique = new Map();
  for (const value of secrets) {
    const secret = copyBytes(value, "response secret");
    if (secret.length === 0) {
      throw new TypeError("response secrets must not be empty");
    }
    // A replacement must never itself reproduce the value being removed.
    if (MARKER_BYTES.indexOf(secret) !== -1) {
      throw new TypeError("a response secret conflicts with the redaction marker");
    }
    unique.set(secret.toString("hex"), secret);
  }

  // Longest-first gives deterministic leftmost-longest behavior for secrets
  // that share a prefix. The hex tie-breaker makes compilation reproducible.
  return Object.freeze(
    [...unique.values()].sort(
      (left, right) => right.length - left.length || Buffer.compare(left, right),
    ),
  );
}


function compiledSecrets(secrets) {
  const normalized = normalizeResponseSecrets(secrets);
  const byFirstByte = new Map();
  let maxLength = 0;
  for (const secret of normalized) {
    maxLength = Math.max(maxLength, secret.length);
    const patterns = byFirstByte.get(secret[0]) ?? [];
    patterns.push(secret);
    byFirstByte.set(secret[0], patterns);
  }
  return { normalized, byFirstByte, maxLength };
}


function matchAt(bytes, offset, patterns) {
  if (!patterns) return null;
  for (const secret of patterns) {
    if (
      offset + secret.length <= bytes.length
      && bytes.subarray(offset, offset + secret.length).equals(secret)
    ) {
      return secret;
    }
  }
  return null;
}


export function createResponseSecretRedactor(secrets) {
  const { byFirstByte, maxLength } = compiledSecrets(secrets);
  let pending = EMPTY;
  let state = "open";

  function requireOpen() {
    if (state !== "open") {
      throw new Error(`response redactor is ${state}`);
    }
  }

  function process(bytes, final) {
    if (maxLength === 0) return bytes;

    // Before flush, do not decide anything about the final maxLength - 1
    // bytes: they may be the beginning of a secret split across chunks.
    const safeStartLimit = final
      ? bytes.length
      : Math.max(0, bytes.length - maxLength + 1);
    const parts = [];
    let outputLength = 0;
    let scan = 0;
    let plainStart = 0;

    while (scan < safeStartLimit) {
      const secret = matchAt(bytes, scan, byFirstByte.get(bytes[scan]));
      if (!secret) {
        scan += 1;
        continue;
      }
      if (plainStart < scan) {
        const plain = bytes.subarray(plainStart, scan);
        parts.push(plain);
        outputLength += plain.length;
      }
      parts.push(MARKER_BYTES);
      outputLength += MARKER_BYTES.length;
      scan += secret.length;
      plainStart = scan;
    }

    if (plainStart < scan) {
      const plain = bytes.subarray(plainStart, scan);
      parts.push(plain);
      outputLength += plain.length;
    }
    pending = final ? EMPTY : Buffer.from(bytes.subarray(scan));
    return outputLength === 0 ? EMPTY : Buffer.concat(parts, outputLength);
  }

  function push(chunk) {
    requireOpen();
    try {
      const incoming = copyBytes(chunk, "response chunk", false);
      const bytes = pending.length === 0
        ? incoming
        : Buffer.concat([pending, incoming], pending.length + incoming.length);
      return process(bytes, false);
    } catch (error) {
      pending = EMPTY;
      state = "failed";
      throw error;
    }
  }

  function flush() {
    requireOpen();
    try {
      const output = process(pending, true);
      pending = EMPTY;
      state = "closed";
      return output;
    } catch (error) {
      pending = EMPTY;
      state = "failed";
      throw error;
    }
  }

  return Object.freeze({
    push,
    flush,
    get pendingBytes() {
      return pending.length;
    },
  });
}


function headerValueContainsSecret(value, secrets) {
  const values = Array.isArray(value) ? value : [value];
  if (values.length === 0) return true;
  for (const item of values) {
    let bytes;
    try {
      bytes = copyBytes(item, "response header value");
    } catch {
      // A header value we cannot inspect must not cross the boundary.
      return true;
    }
    if (secrets.some((secret) => bytes.indexOf(secret) !== -1)) return true;
  }
  return false;
}


function responseHeaderEntries(headers) {
  if (!headers || typeof headers === "string" || Buffer.isBuffer(headers)) {
    throw new TypeError("response headers must be an object or iterable");
  }
  if (typeof headers[Symbol.iterator] === "function") return headers;
  if (typeof headers.entries === "function") return headers.entries();
  if (typeof headers === "object") return Object.entries(headers);
  throw new TypeError("response headers must be an object or iterable");
}


export function filterResponseHeaders(headers, secrets) {
  const normalized = normalizeResponseSecrets(secrets);
  const filtered = Object.create(null);
  const blocked = new Set();

  for (const entry of responseHeaderEntries(headers)) {
    if (!Array.isArray(entry) || entry.length !== 2) continue;
    const [rawName, value] = entry;
    if (typeof rawName !== "string" || !HEADER_NAME.test(rawName)) continue;
    const name = rawName.toLowerCase();
    if (blocked.has(name)) continue;
    if (headerValueContainsSecret(value, normalized)) {
      blocked.add(name);
      delete filtered[name];
      continue;
    }
    // Copy arrays so a caller cannot mutate a retained header after filtering.
    filtered[name] = Array.isArray(value) ? [...value] : value;
  }
  return filtered;
}
