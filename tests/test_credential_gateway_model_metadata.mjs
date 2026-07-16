import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { sanitizeModel } from "../src/cyclo/credential_gateway/gateway_context/model-metadata.mjs";
import {
  SAFE_MODEL_FIELDS_URL,
  loadSafeModelFields,
  safeModelFields,
} from "../src/cyclo/credential_gateway/gateway_context/safe-model-fields.mjs";


test("the Node gateway consumes the canonical safe-field manifest", async () => {
  const document = JSON.parse(await readFile(SAFE_MODEL_FIELDS_URL, "utf8"));
  assert.deepEqual(safeModelFields, document);
  assert.equal(Object.isFrozen(safeModelFields), true);
  for (const [key, value] of Object.entries(safeModelFields)) {
    if (key !== "schemaVersion") assert.equal(Object.isFrozen(value), true, key);
  }
});


test("the safe-field manifest schema fails closed", async () => {
  const valid = JSON.parse(await readFile(SAFE_MODEL_FIELDS_URL, "utf8"));
  const cases = [
    [[], /must be a JSON object/],
    [{ ...valid, schemaVersion: 2 }, /requires schemaVersion 1/],
    [Object.fromEntries(Object.entries(valid).filter(([key]) => key !== "costFields")), /invalid keys/],
    [{ ...valid, unknownFields: ["unsafe"] }, /invalid keys/],
    [{ ...valid, inputTypes: "text" }, /must be a non-empty array/],
    [{ ...valid, inputTypes: [] }, /must be a non-empty array/],
    [{ ...valid, inputTypes: ["text", ""] }, /only non-empty strings/],
    [{ ...valid, inputTypes: ["text", "text"] }, /must not contain duplicates/],
  ];
  const directory = await mkdtemp(join(tmpdir(), "cyclo-safe-fields-"));
  const path = join(directory, "manifest.json");
  try {
    for (const [document, message] of cases) {
      await writeFile(path, `${JSON.stringify(document)}\n`, "utf8");
      assert.throws(() => loadSafeModelFields(path), message);
    }
    await writeFile(path, "{", "utf8");
    assert.throws(() => loadSafeModelFields(path), /cannot load safe model fields manifest/);
    assert.throws(
      () => loadSafeModelFields(join(directory, "missing.json")),
      /cannot load safe model fields manifest/,
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});


test("model metadata projection uses every canonical allowlist and drops unsafe fields", () => {
  const cost = Object.fromEntries(
    safeModelFields.costFields.map((field, index) => [field, index]),
  );
  cost.authorization = "secret";
  const compat = Object.fromEntries(
    safeModelFields.compatBooleanFields.map((field) => [field, true]),
  );
  compat.maxTokensField = safeModelFields.maxTokensFields[0];
  compat.thinkingFormat = safeModelFields.thinkingFormats[0];
  compat.cacheControlFormat = safeModelFields.cacheControlFormats[0];
  compat.headers = { authorization: "secret" };
  const thinkingLevelMap = Object.fromEntries(
    safeModelFields.thinkingLevels.map((field) => [field, `mapped-${field}`]),
  );
  thinkingLevelMap.unsafe = "secret";

  const projected = sanitizeModel({
    id: "model",
    name: "Model",
    provider: "provider",
    api: "openai-completions",
    reasoning: true,
    input: [...safeModelFields.inputTypes, "audio", { apiKey: "secret" }],
    contextWindow: 1000,
    maxTokens: 100,
    cost,
    compat,
    thinkingLevelMap,
    apiKey: "secret",
    baseUrl: "https://provider.invalid",
    headers: { authorization: "secret" },
  });

  assert.deepEqual(projected.input, safeModelFields.inputTypes);
  assert.deepEqual(Object.keys(projected.cost), safeModelFields.costFields);
  assert.deepEqual(
    Object.keys(projected.compat),
    [
      ...safeModelFields.compatBooleanFields,
      "maxTokensField",
      "thinkingFormat",
      "cacheControlFormat",
    ],
  );
  assert.deepEqual(Object.keys(projected.thinkingLevelMap), safeModelFields.thinkingLevels);
  assert.equal(JSON.stringify(projected).includes("secret"), false);
  assert.equal("apiKey" in projected, false);
  assert.equal("baseUrl" in projected, false);
  assert.equal("headers" in projected, false);

  const enumFields = {
    maxTokensField: safeModelFields.maxTokensFields,
    thinkingFormat: safeModelFields.thinkingFormats,
    cacheControlFormat: safeModelFields.cacheControlFormats,
  };
  for (const [compatField, allowedValues] of Object.entries(enumFields)) {
    for (const allowedValue of allowedValues) {
      assert.deepEqual(
        sanitizeModel({ id: "model", compat: { [compatField]: allowedValue } }),
        { id: "model", compat: { [compatField]: allowedValue } },
      );
    }
  }
});
