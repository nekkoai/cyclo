import assert from "node:assert/strict";
import test from "node:test";

import { createTextRedactor, redactValue } from "../src/secrets.mjs";

test("redacts a credential split across arbitrary native deltas", () => {
  const redactor = createTextRedactor(["secret-token"]);
  const output = [
    redactor.push("before sec"),
    redactor.push("ret-"),
    redactor.push("token after"),
    redactor.flush(),
  ].join("");
  assert.equal(output, "before [REDACTED] after");
});

test("redacts nested tool values and rejects credential-bearing keys", () => {
  assert.deepEqual(
    { ...redactValue({ nested: ["secret-token", 3] }, ["secret-token"]) },
    { nested: ["[REDACTED]", 3] },
  );
  assert.throws(
    () => redactValue({ "secret-token": true }, ["secret-token"]),
    /key contains a credential/u,
  );
});
