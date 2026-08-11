import assert from "node:assert/strict";
import test from "node:test";

import {
  outputModelId,
  parseArguments,
  parseComponentName,
  publicModelId,
} from "../src/config.mjs";

test("configuration accepts exact-model and provider-wide pools", () => {
  const exact = parseArguments([
    "codex-one/gpt-5.4",
    "codex_two/family/gpt=5.4",
    "model=balanced/codex",
  ]);
  assert.deepEqual(exact, {
    memberModelIds: ["codex-one/gpt-5.4", "codex_two/family/gpt=5.4"],
    outputModel: "balanced/codex",
  });
  assert.equal(Object.isFrozen(exact.memberModelIds), true);

  const providers = parseArguments(["codex-two", "codex_one", "codex-three"]);
  assert.deepEqual(providers, {
    memberProviders: ["codex-two", "codex_one", "codex-three"],
  });
  assert.equal(Object.isFrozen(providers.memberProviders), true);
});

test("configuration rejects ambiguity and unsupported parameters", () => {
  assert.throws(() => parseArguments([]), /at least two/u);
  assert.throws(() => parseArguments(["one/a", "model=x"]), /at least two/u);
  assert.throws(() => parseArguments(["one/a", "one/a", "model=x"]), /distinct/u);
  assert.throws(() => parseArguments(["one/a", "two/b"]), /requires model=/u);
  assert.throws(() => parseArguments(["one/a", "two/b", "model="]), /output model/u);
  assert.throws(
    () => parseArguments(["one/a", "two/b", "strategy=round-robin", "model=x"]),
    /unknown pooler parameter/u,
  );
  assert.throws(
    () => parseArguments(["one/a", "two/b", "model=x", "three/c"]),
    /must precede/u,
  );
  assert.throws(
    () => parseArguments(["one/a", "two/b", "model=x", "model=y"]),
    /duplicate/u,
  );
  assert.throws(() => parseArguments(["one", "one"]), /distinct/u);
  assert.throws(() => parseArguments(["one", "two/model"]), /cannot mix/u);
  assert.throws(
    () => parseArguments(["one", "two", "model=balanced"]),
    /only valid with member model IDs/u,
  );
});

test("Provider public IDs and DComp component names use shared strict rules", () => {
  assert.equal(publicModelId("account_2/family/model"), "account_2/family/model");
  assert.equal(parseComponentName("quota-pool"), "quota-pool");
  assert.equal(outputModelId("quota-pool", "balanced"), "quota-pool/balanced");

  assert.throws(() => publicModelId("missing-provider"), /PROVIDER\/MODEL/u);
  assert.throws(() => publicModelId("_account/model"), /provider/u);
  assert.throws(() => publicModelId("gateway/model"), /provider/u);
  assert.throws(() => publicModelId("account/white space"), /local model/u);
  assert.throws(() => publicModelId(`account/${"x".repeat(1_025)}`), /local model/u);
  assert.throws(() => parseComponentName("Uppercase"), /DCOMP_COMPONENT_NAME/u);
  assert.throws(() => parseComponentName("under_score"), /DCOMP_COMPONENT_NAME/u);
  assert.throws(() => parseComponentName(`a${"b".repeat(63)}`), /DCOMP_COMPONENT_NAME/u);
});
