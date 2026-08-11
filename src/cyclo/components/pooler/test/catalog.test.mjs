import assert from "node:assert/strict";
import test from "node:test";

import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { composeCatalog } from "../src/catalog.mjs";
import { parseArguments } from "../src/config.mjs";

function capabilities(overrides = {}) {
  return {
    inputModalities: [1, 2],
    outputModalities: [1],
    functionTools: true,
    parallelToolCalls: true,
    reasoningSummaries: true,
    temperature: false,
    topP: false,
    stopSequences: false,
    extensionTypes: ["example.Feature"],
    reasoning: true,
    ...overrides,
  };
}

function model(id, overrides = {}) {
  return {
    id,
    displayName: id,
    capabilities: capabilities(),
    contextWindowTokens: 128_000n,
    maxOutputTokens: 16_000n,
    extensions: [{
      typeUrl: "type.googleapis.com/example.Feature",
      value: new Uint8Array([1, 2]),
    }],
    inferenceFormat: PI_INFERENCE_FORMAT,
    ...overrides,
  };
}

const exactConfig = () => parseArguments([
  "codex-one/gpt",
  "codex-two/gpt",
  "model=balanced",
]);

test("exact composition preserves upstream and appends a conservative model", () => {
  const first = model("codex-one/gpt", {
    contextWindowTokens: 200_000n,
    maxOutputTokens: 8_000n,
  });
  const unrelated = model("other/model");
  const second = model("codex-two/gpt", {
    contextWindowTokens: 100_000n,
    maxOutputTokens: 12_000n,
  });
  const result = composeCatalog([first, unrelated, second], exactConfig(), "pool");

  assert.deepEqual(result.models.slice(0, 3), [first, unrelated, second]);
  assert.deepEqual(result.pools[0].memberModelIds, [
    "codex-one/gpt",
    "codex-two/gpt",
  ]);
  assert.deepEqual(result.pools[0].virtualModel, {
    id: "pool/balanced",
    displayName: "pool/balanced",
    capabilities: capabilities(),
    contextWindowTokens: 100_000n,
    maxOutputTokens: 8_000n,
    extensions: [{
      typeUrl: "type.googleapis.com/example.Feature",
      value: new Uint8Array([1, 2]),
    }],
    inferenceFormat: PI_INFERENCE_FORMAT,
  });
  assert.notEqual(result.pools[0].virtualModel.capabilities, first.capabilities);
  assert.notEqual(result.pools[0].virtualModel.extensions, first.extensions);
});

test("provider-wide composition creates ordered pools for shared local IDs", () => {
  const upstream = [
    model("codex-one/gpt", { contextWindowTokens: 200_000n }),
    model("codex-one/family/embed", { maxOutputTokens: 4_000n }),
    model("codex-one/only"),
    model("other/gpt"),
    model("codex-two/gpt", { contextWindowTokens: 100_000n }),
    model("codex-two/family/embed", { maxOutputTokens: 8_000n }),
  ];
  const result = composeCatalog(
    upstream,
    parseArguments(["codex-two", "codex-one"]),
    "pool",
  );

  assert.deepEqual(result.models.map((entry) => entry.id), [
    "codex-one/gpt",
    "codex-one/family/embed",
    "codex-one/only",
    "other/gpt",
    "codex-two/gpt",
    "codex-two/family/embed",
    "pool/gpt",
    "pool/family/embed",
  ]);
  assert.deepEqual(
    result.pools.map((pool) => ({
      output: pool.virtualModel.id,
      members: pool.memberModelIds,
    })),
    [
      { output: "pool/gpt", members: ["codex-two/gpt", "codex-one/gpt"] },
      {
        output: "pool/family/embed",
        members: ["codex-two/family/embed", "codex-one/family/embed"],
      },
    ],
  );
});

test("provider-wide composition pools more than two accounts", () => {
  const result = composeCatalog([
    model("account-a/model"),
    model("account-b/model"),
    model("account-c/model"),
    model("account-a/partial"),
    model("account-c/partial"),
  ], parseArguments(["account-c", "account-a", "account-b"]), "pool");

  assert.deepEqual(
    result.pools.map((pool) => ({
      output: pool.virtualModel.id,
      members: pool.memberModelIds,
    })),
    [
      {
        output: "pool/model",
        members: ["account-c/model", "account-a/model", "account-b/model"],
      },
      {
        output: "pool/partial",
        members: ["account-c/partial", "account-a/partial"],
      },
    ],
  );
});

test("composition rejects unavailable members, collisions, and unusable providers", () => {
  const upstream = [model("codex-one/gpt"), model("codex-two/gpt")];
  assert.throws(
    () => composeCatalog(upstream.slice(0, 1), exactConfig(), "pool"),
    /member model is unavailable: codex-two\/gpt/u,
  );
  assert.throws(
    () => composeCatalog([...upstream, model("pool/balanced")], exactConfig(), "pool"),
    /collides/u,
  );
  assert.throws(
    () => composeCatalog([...upstream, model("codex-one/gpt")], exactConfig(), "pool"),
    /repeats model/u,
  );
  assert.throws(
    () => composeCatalog(
      upstream,
      parseArguments(["codex-one", "missing"]),
      "pool",
    ),
    (error) => {
      assert.match(error.message, /member provider "missing" is unavailable/u);
      assert.match(error.message, /configured providers: codex-one, missing/u);
      assert.match(error.message, /available upstream providers: codex-one, codex-two/u);
      return true;
    },
  );
  assert.throws(
    () => composeCatalog(
      [model("codex-one/gpt"), model("codex-two/other")],
      parseArguments(["codex-one", "codex-two"]),
      "pool",
    ),
    /provider-local model IDs must match exactly/u,
  );
});

test("composition refuses incompatible or invalid Provider metadata", () => {
  const first = model("codex-one/gpt");
  const altered = (overrides) => [first, model("codex-two/gpt", overrides)];
  assert.throws(
    () => composeCatalog(altered({ inferenceFormat: "another-abi" }), exactConfig(), "pool"),
    /incompatible inference formats/u,
  );
  assert.throws(
    () => composeCatalog(
      altered({ capabilities: capabilities({ reasoning: false }) }),
      exactConfig(),
      "pool",
    ),
    /incompatible capabilities/u,
  );
  assert.throws(
    () => composeCatalog(altered({ extensions: [] }), exactConfig(), "pool"),
    /incompatible extensions/u,
  );
  assert.throws(
    () => composeCatalog(altered({ contextWindowTokens: 0n }), exactConfig(), "pool"),
    /positive uint64/u,
  );
  assert.throws(
    () => composeCatalog([model("codex-one/gpt"), model("invalid id")], exactConfig(), "pool"),
    /PROVIDER\/MODEL/u,
  );
});
