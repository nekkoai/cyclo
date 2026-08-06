import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { Code, ConnectError } from "@connectrpc/connect";
import {
  createResourceExhaustedError,
  resourceExhaustedRetryAt,
} from "@cyclo/provider/errors";
import { ResourceExhaustionSchema } from "@cyclo/provider/contract";
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

test("constructs and reads the typed absolute resource-exhaustion deadline", () => {
  const retryAt = new Date("2031-02-03T04:05:06.789Z");
  const error = createResourceExhaustedError(retryAt);

  assert.equal(error.code, Code.ResourceExhausted);
  assert.equal(error.rawMessage, "provider resource exhausted");
  assert.equal(resourceExhaustedRetryAt(error)?.toISOString(), retryAt.toISOString());
  assert.equal(resourceExhaustedRetryAt(new ConnectError("busy", Code.Unavailable)), undefined);
  assert.throws(() => createResourceExhaustedError(new Date(Number.NaN)), /valid Date/u);
  assert.throws(() => createResourceExhaustedError(new Date("+010000-01-01T00:00:00Z")), /Timestamp range/u);
});

test("rejects malformed or ambiguous resource-exhaustion details", () => {
  const malformed = new ConnectError(
    "busy",
    Code.ResourceExhausted,
    undefined,
    [{
      desc: ResourceExhaustionSchema,
      value: { retryAt: { seconds: 1n, nanos: 1_000_000_000 } },
    }],
  );
  assert.equal(resourceExhaustedRetryAt(malformed), undefined);

  const retryAt = new Date("2031-02-03T04:05:06.789Z");
  const valid = createResourceExhaustedError(retryAt);
  valid.details.push(valid.details[0]);
  assert.equal(resourceExhaustedRetryAt(valid), undefined);

  const undecodable = createResourceExhaustedError(retryAt);
  undecodable.findDetails = () => { throw new Error("invalid wire detail"); };
  assert.equal(resourceExhaustedRetryAt(undecodable), undefined);
});
