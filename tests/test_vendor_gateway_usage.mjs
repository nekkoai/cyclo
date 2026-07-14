import { test } from "node:test";
import assert from "node:assert/strict";

import {
  aggregateUsageRecords,
  modelFromRequest,
  usageFromCapture,
} from "../src/cyclo/vendor_gateway/gateway_context/usage.mjs";

test("usage parsing never retains request bodies", () => {
  const body = Buffer.from(JSON.stringify({ model: "gpt-test", messages: [{ content: "secret" }] }));
  const response = Buffer.from(JSON.stringify({
    usage: {
      prompt_tokens: 12,
      completion_tokens: 5,
      prompt_tokens_details: { cached_tokens: 3 },
    },
  }));

  assert.equal(modelFromRequest(body), "gpt-test");
  assert.deepEqual(usageFromCapture(response, "application/json"), {
    input_tokens: 12,
    output_tokens: 5,
    cache_read_tokens: 3,
    cache_write_tokens: 0,
    usage_source: "provider",
    capture_truncated: false,
  });
  assert.equal(JSON.stringify(usageFromCapture(response, "application/json")).includes("secret"), false);
});

test("usage aggregates by Cyclo binding dimensions", () => {
  const record = {
    client_id: "project-a",
    team_id: "team-a",
    binding_generation: "generation-a",
    provider: "claude-work",
    model: "claude-test",
    status: 200,
    request_bytes: 100,
    response_bytes: 250,
    input_tokens: 20,
    output_tokens: 9,
    cache_read_tokens: 7,
    cache_write_tokens: 2,
  };
  const aggregate = aggregateUsageRecords([record, { ...record, status: 429 }]);

  assert.equal(aggregate.totals.requests, 2);
  assert.equal(aggregate.by_client["project-a"].output_tokens, 18);
  assert.equal(aggregate.by_team["team-a"].requests, 2);
  assert.equal(aggregate.by_generation["generation-a"].input_tokens, 40);
  assert.equal(aggregate.by_provider["claude-work"].cache_read_tokens, 14);
  assert.equal(aggregate.by_model["claude-test"].response_bytes, 500);
});
