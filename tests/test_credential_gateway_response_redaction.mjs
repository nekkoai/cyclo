import assert from "node:assert/strict";
import { test } from "node:test";

import {
  REDACTION_MARKER,
  createResponseSecretRedactor,
  filterResponseHeaders,
  normalizeResponseSecrets,
} from "../src/cyclo/credential_gateway/gateway_context/response-redaction.mjs";


const MARKER = "[REDACTED]";


function redactChunks(secrets, chunks) {
  const redactor = createResponseSecretRedactor(secrets);
  const output = [];
  for (const chunk of chunks) {
    output.push(redactor.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk));
  }
  output.push(redactor.flush());
  return Buffer.concat(output).toString("utf8");
}


test("response redaction uses the fixed marker and accepts Buffer and Uint8Array", () => {
  assert.equal(REDACTION_MARKER.toString("ascii"), MARKER);
  const secret = new Uint8Array(Buffer.from("credential-value"));
  const input = new Uint8Array(Buffer.from("before credential-value after"));

  assert.equal(redactChunks([secret], [input]), `before ${MARKER} after`);
});


test("secrets are redacted across every possible chunk boundary", () => {
  const input = Buffer.from("prefix-super-secret-suffix");
  for (let split = 0; split <= input.length; split += 1) {
    const chunks = [input.subarray(0, split), input.subarray(split)];
    assert.equal(
      redactChunks([Buffer.from("super-secret")], chunks),
      `prefix-${MARKER}-suffix`,
      `split at byte ${split}`,
    );
  }
});


test("prefix and overlapping patterns use leftmost-longest redaction", () => {
  assert.equal(
    redactChunks(["abc", "abcd", "bcd", "aba", "bab"], ["xxabcd-ababa-abc"]),
    `xx${MARKER}-${MARKER}ba-${MARKER}`,
  );
  assert.equal(redactChunks(["token", "token-long"], ["token-", "long"]), MARKER);
});


test("adjacent and repeated matches are all redacted", () => {
  assert.equal(
    redactChunks(["key", "other"], ["k", "eykeyot", "her"]),
    `${MARKER}${MARKER}${MARKER}`,
  );
});


test("retained state is bounded by the longest secret rather than stream size", () => {
  const redactor = createResponseSecretRedactor(["a-credential-that-may-span-chunks"]);
  let emitted = 0;
  for (let index = 0; index < 10_000; index += 1) {
    emitted += redactor.push(Buffer.alloc(128, 0x78)).length;
    assert.ok(redactor.pendingBytes < "a-credential-that-may-span-chunks".length);
  }
  emitted += redactor.flush().length;
  assert.equal(emitted, 1_280_000);
});


test("empty secret lists stream bytes unchanged", () => {
  assert.equal(redactChunks([], [Buffer.from([0, 1]), new Uint8Array([2, 3])]), "\u0000\u0001\u0002\u0003");
});


test("invalid inputs fail closed and a failed redactor cannot resume", () => {
  for (const secrets of [null, "one-secret", [""], [42], [[1, 2, 3]], ["REDACTED"]]) {
    assert.throws(() => createResponseSecretRedactor(secrets));
  }

  const redactor = createResponseSecretRedactor(["credential"]);
  assert.throws(() => redactor.push("not byte input"), /response chunk/);
  assert.throws(() => redactor.push(Buffer.from("credential")), /failed/);
  assert.throws(() => redactor.flush(), /failed/);

  const closed = createResponseSecretRedactor(["credential"]);
  closed.flush();
  assert.throws(() => closed.push(Buffer.alloc(0)), /closed/);
  assert.throws(() => closed.flush(), /closed/);
});


test("normalization copies, deduplicates, and orders secrets longest first", () => {
  const mutable = Buffer.from("short");
  const normalized = normalizeResponseSecrets([mutable, "much-longer", "short"]);
  mutable.fill(0x78);
  assert.deepEqual(normalized.map((secret) => secret.toString()), ["much-longer", "short"]);
  assert.ok(Object.isFrozen(normalized));
});


test("response headers containing a secret are dropped", () => {
  const headers = new Headers([
    ["content-type", "application/json"],
    ["x-safe", "public"],
    ["x-upstream-error", "credential=top-secret"],
    ["www-authenticate", "Bearer top-secret"],
  ]);
  const filtered = filterResponseHeaders(headers, ["top-secret", "Bearer top-secret"]);

  assert.equal(Object.getPrototypeOf(filtered), null);
  assert.deepEqual({ ...filtered }, {
    "content-type": "application/json",
    "x-safe": "public",
  });
});


test("duplicate and malformed response headers fail closed", () => {
  const headers = [
    ["X-Duplicate", "initially safe"],
    ["x-duplicate", new Uint8Array(Buffer.from("contains hidden-key"))],
    ["x-duplicate", "safe again"],
    ["x-array", ["safe", "also hidden-key"]],
    ["bad name", "safe"],
    ["x-unknown", { value: "safe" }],
    ["x-safe", ["one", "two"]],
    ["malformed-entry"],
  ];
  const filtered = filterResponseHeaders(headers, [Buffer.from("hidden-key")]);

  assert.deepEqual({ ...filtered }, { "x-safe": ["one", "two"] });
  assert.throws(() => filterResponseHeaders(null, ["hidden-key"]));
  assert.throws(() => filterResponseHeaders(headers, null));
});
