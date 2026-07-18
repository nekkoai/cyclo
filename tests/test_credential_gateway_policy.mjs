import { test } from "node:test";
import assert from "node:assert/strict";

import {
  clientPrincipalFromRegistryEntry,
  filterModelsForPrincipal,
  isKnownInferenceEndpoint,
  modelFromGooglePath,
  modelFromInferenceRequest,
  principalAllowsModel,
} from "../src/cyclo/credential_gateway/gateway_context/policy.mjs";

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
    "/v1/models/gemini-2.5-pro:generateContent",
    "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
    "/v1/projects/project-a/locations/us-central1/publishers/google/models/gemini-2.5-pro:generateContent",
    "/v1internal:streamGenerateContent",
    "/v1internal/generateContent",
  ]) {
    assert.equal(isKnownInferenceEndpoint("POST", path), true, path);
  }

  for (const [method, path] of [
    ["GET", "/responses"],
    ["DELETE", "/v1/messages"],
    ["POST", "/models"],
    ["POST", "/v1/files"],
    ["POST", "/v1/messages/count_tokens"],
    ["POST", "/admin/models/gemini-2.5-pro:generateContent"],
    ["POST", "/v1beta/extra/models/gemini-2.5-pro:generateContent"],
    ["POST", "/v1/projects/a/locations/b/publishers/other/models/gemini:generateContent"],
    ["POST", "/v1beta/models/../gemini:generateContent"],
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
  assert.equal(
    modelFromGooglePath("/v1beta/models/publisher%2F..%2Fmodel:generateContent"),
    null,
  );
});

test("body model extraction rejects duplicate keys and invalid UTF-8", () => {
  assert.equal(
    modelFromInferenceRequest(
      "/responses",
      Buffer.from('{"model":"allowed","input":[],"nested":{"model":"ignored"}}'),
    ),
    "allowed",
  );
  assert.equal(
    modelFromInferenceRequest(
      "/responses",
      Buffer.from('{"model":"first","model":"second"}'),
    ),
    null,
  );
  assert.equal(
    modelFromInferenceRequest(
      "/responses",
      Buffer.from('{"mo\\u0064el":"first","model":"second"}'),
    ),
    null,
  );
  assert.equal(
    modelFromInferenceRequest("/responses", Buffer.from([0x7b, 0x22, 0xff, 0x22, 0x3a, 0x31, 0x7d])),
    null,
  );
  assert.equal(
    modelFromInferenceRequest("/responses", Buffer.from('{"model":42}')),
    null,
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

test("client registry kinds cannot create a privileged gateway principal", () => {
  const principal = clientPrincipalFromRegistryEntry({
    kind: "provider",
    provider_prefix: "fusion",
    client_id: "team-one",
    team_id: "team-one",
    binding_generation: "generation-one",
    providers: ["upstream"],
    models: ["upstream/input-model"],
  });

  assert.equal(principal.kind, "client");
  assert.equal(Object.hasOwn(principal, "provider_prefix"), false);
  assert.equal(principal.binding_generation, "generation-one");
  assert.equal(principalAllowsModel(principal, "upstream", "input-model"), true);
  assert.equal(principalAllowsModel(principal, "upstream", "other-model"), false);
  assert.equal(principalAllowsModel(principal, "fusion", "input-model"), false);
});
