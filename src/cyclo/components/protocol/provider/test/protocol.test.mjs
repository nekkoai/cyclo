import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  decodePayload,
  encodePayload,
  isLocalModelId,
  isProviderPrefix,
  PI_INFERENCE_FORMAT,
  splitPublicModelId,
} from "@cyclo/provider/protocol";

const MODEL_ID_CASES = JSON.parse(await readFile(
  new URL("./model-id-cases.json", import.meta.url),
  "utf8",
));

test("identifies the pinned Pi payload ABI", () => {
  assert.equal(PI_INFERENCE_FORMAT, "pi-ai@0.81.1");
});

test("defines one bounded PROVIDER/MODEL public-ID contract", () => {
  for (const fixture of MODEL_ID_CASES) {
    const id = fixture.prefix + fixture.unit.repeat(fixture.repeat) + fixture.suffix;
    assert.equal(Boolean(splitPublicModelId(id)), fixture.valid, fixture.name);
  }
  assert.equal(isProviderPrefix("work_1"), true);
  assert.equal(isProviderPrefix("_work"), false);
  assert.equal(isLocalModelId("family/model"), true);
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
