import assert from "node:assert/strict";
import { test } from "node:test";
import { zstdCompressSync } from "node:zlib";

import {
  forwardedRequestHeaders,
  prepareRequestBody,
} from "../src/cyclo/vendor_gateway/gateway_context/request-body.mjs";
import {
  modelFromInferenceRequest,
  principalAllowsModel,
} from "../src/cyclo/vendor_gateway/gateway_context/policy.mjs";


function isRequestError(statusCode, message) {
  return (error) => {
    assert.equal(error?.statusCode, statusCode);
    assert.match(error?.message ?? "", message);
    return true;
  };
}


test("zstd Pi bodies are decoded for model scope and preserved for forwarding", async () => {
  const decoded = Buffer.from(JSON.stringify({
    model: "gpt-5-codex",
    stream: true,
    input: "do not retain this request",
  }));
  const compressed = zstdCompressSync(decoded);
  const originalCompressed = Buffer.from(compressed);

  const { policyBody, upstreamBody } = await prepareRequestBody(
    compressed,
    "zstd",
    1024 * 1024,
  );
  const model = modelFromInferenceRequest("/codex/responses", policyBody);

  assert.equal(model, "gpt-5-codex");
  assert.equal(
    principalAllowsModel(
      { kind: "client", models: ["openai-codex/gpt-5-codex"] },
      "openai-codex",
      model,
    ),
    true,
  );
  assert.strictEqual(upstreamBody, compressed);
  assert.deepEqual(upstreamBody, originalCompressed);
  assert.notDeepEqual(upstreamBody, policyBody);

  const headers = forwardedRequestHeaders({
    accept: "text/event-stream",
    authorization: "Bearer client-capability",
    "content-encoding": "zstd",
    "content-length": String(compressed.length),
    "content-type": "application/json",
  });
  assert.deepEqual(headers, {
    "accept-encoding": "identity",
    accept: "text/event-stream",
    "content-encoding": "zstd",
    "content-type": "application/json",
  });
});


test("identity request bodies retain the same policy and upstream bytes", async () => {
  const body = Buffer.from(JSON.stringify({ model: "gpt-5-codex" }));

  for (const encoding of [undefined, "identity", " IDENTITY "]) {
    const prepared = await prepareRequestBody(body, encoding, body.length);
    assert.strictEqual(prepared.policyBody, body);
    assert.strictEqual(prepared.upstreamBody, body);
  }
});


test("zstd policy decoding enforces its output bound", async () => {
  const compressed = zstdCompressSync(Buffer.alloc(1024, 0x61));

  await assert.rejects(
    prepareRequestBody(compressed, "zstd", 1023),
    isRequestError(413, /decoded request body too large/),
  );
});


test("unsupported and malformed content encodings fail closed", async () => {
  const jsonBody = Buffer.from(JSON.stringify({ model: "gpt-5-codex" }));

  for (const encoding of ["gzip", "br", "zstd, identity"]) {
    await assert.rejects(
      prepareRequestBody(jsonBody, encoding, 1024),
      isRequestError(415, /unsupported content-encoding/),
    );
  }
  for (const encoding of ["", "zstd,", "zstd;level=1"]) {
    await assert.rejects(
      prepareRequestBody(jsonBody, encoding, 1024),
      isRequestError(400, /malformed content-encoding/),
    );
  }
  await assert.rejects(
    prepareRequestBody(jsonBody, "zstd", 1024),
    isRequestError(400, /malformed zstd request body/),
  );
});
