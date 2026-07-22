import assert from "node:assert/strict";
import test from "node:test";

import {
  decodePayload,
  encodePayload,
  PI_INFERENCE_FORMAT,
} from "@cyclo/provider/protocol";

test("identifies the pinned Pi payload ABI", () => {
  assert.equal(PI_INFERENCE_FORMAT, "pi-ai@0.81.1");
});

test("encodes and decodes arbitrary JSON without inference semantics", () => {
  const value = {
    context: {
      messages: [{ role: "future-role", content: "\u2603" }],
      tools: [{
        name: "anything goes",
        parameters: {
          anyOf: [{ type: "boolean" }, { enum: ["always", "never"] }],
          "x-unknown": { nested: [null, true, 3.5] },
          __proto__: null,
        },
      }],
    },
    options: { futureProviderOption: { enabled: true } },
  };

  assert.equal(
    JSON.stringify(decodePayload(encodePayload(value))),
    JSON.stringify(value),
  );
});

test("rejects only values that JSON itself cannot transport", () => {
  assert.throws(() => encodePayload(undefined), /not JSON-serializable/u);
  assert.throws(() => encodePayload(1n), /BigInt/u);
  assert.throws(() => decodePayload("not-json"), SyntaxError);
});
