import { test } from "node:test";
import assert from "node:assert/strict";

import {
  clientPrincipalFromRegistryEntry,
  filterModelsForPrincipal,
  isKnownInferenceEndpoint,
  modelFromGooglePath,
  modelFromInferenceRequest,
  principalAllowsModel,
} from "../src/cyclo/vendor_gateway/gateway_context/policy.mjs";

test("only current Pi inference POST endpoints are accepted", () => {
  for (const path of [
    "/chat/completions",
    "/v1/chat/completions",
    "/responses",
    "/v1/responses",
    "/codex/responses",
    "/messages",
    "/v1/messages",
    "/models/gemini-2.5-pro:generateContent",
    "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
    "/v1internal:streamGenerateContent",
  ]) {
    assert.equal(isKnownInferenceEndpoint("POST", path), true, path);
  }

  for (const [method, path] of [
    ["GET", "/responses"],
    ["DELETE", "/v1/messages"],
    ["POST", "/models"],
    ["POST", "/v1/files"],
    ["POST", "/v1/messages/count_tokens"],
    ["POST", "/anything/else"],
  ]) {
    assert.equal(isKnownInferenceEndpoint(method, path), false, `${method} ${path}`);
  }
});

test("model extraction preserves exact ids containing slashes", () => {
  assert.equal(
    modelFromGooglePath("/v1beta/models/publisher%2Ffamily%2Fmodel:streamGenerateContent"),
    "publisher/family/model",
  );
  assert.equal(
    modelFromInferenceRequest(
      "/responses",
      Buffer.from(JSON.stringify({ model: "org/family/model" })),
    ),
    "org/family/model",
  );
});

test("client model authorization is exact while admin remains unrestricted", () => {
  const client = {
    kind: "client",
    models: ["openai/org/model", "google/publisher/family/model"],
  };
  assert.equal(principalAllowsModel(client, "openai", "org/model"), true);
  assert.equal(principalAllowsModel(client, "openai", "org/model-plus"), false);
  assert.equal(principalAllowsModel(client, "google", "publisher/family/model"), true);
  assert.equal(principalAllowsModel(client, "anthropic", "org/model"), false);
  assert.equal(principalAllowsModel({ kind: "admin" }, "any", "model"), true);
});

test("registry model scopes flow into authentication and catalog filtering", () => {
  const principal = clientPrincipalFromRegistryEntry({
    client_id: "run-a",
    team_id: "team-a",
    binding_generation: "generation-a",
    providers: ["openai"],
    models: ["openai/org/allowed"],
  });
  assert.deepEqual(principal.models, ["openai/org/allowed"]);
  assert.deepEqual(
    filterModelsForPrincipal(principal, "openai", [
      { id: "org/allowed" },
      { id: "org/denied" },
    ]),
    [{ id: "org/allowed" }],
  );
});
