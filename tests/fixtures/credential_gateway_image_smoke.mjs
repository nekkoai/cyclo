// Runs inside the built gateway image. This proves the shared allowlist files
// were copied into /app and that the real server catalog path strips secrets.

import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";

const authPath = process.env.CYCLO_GATEWAY_AUTH_JSON;
const modelsPath = process.env.CYCLO_GATEWAY_MODELS_JSON;
assert.ok(authPath);
assert.ok(modelsPath);

await writeFile(
  authPath,
  `${JSON.stringify({
    account: { type: "api_key", provider: "custom", key: "credential-secret" },
  })}\n`,
  { encoding: "utf8", mode: 0o600 },
);
await writeFile(
  modelsPath,
  `${JSON.stringify({
    providers: {
      custom: {
        baseUrl: "https://provider.invalid/v1",
        api: "openai-completions",
        models: [
          {
            id: "safe-model",
            input: ["text", "credential-secret"],
            cost: { input: 1, authorization: "credential-secret" },
            compat: {
              supportsStore: true,
              headers: { authorization: "credential-secret" },
            },
            apiKey: "credential-secret",
            baseUrl: "https://credential-secret.invalid",
            headers: { authorization: "credential-secret" },
          },
        ],
      },
    },
  })}\n`,
  { encoding: "utf8", mode: 0o600 },
);

const { safeModelFields } = await import("/app/safe-model-fields.mjs");
const { sanitizeModel } = await import("/app/model-metadata.mjs");
assert.equal(sanitizeModel({ id: "direct-smoke" }).id, "direct-smoke");
assert.equal(safeModelFields.schemaVersion, 1);

const { providerCatalog } = await import("/app/server.mjs");
const catalog = providerCatalog();
assert.deepEqual(catalog, {
  account: {
    api: "openai-completions",
    models: [
      {
        id: "safe-model",
        input: ["text"],
        cost: { input: 1 },
        compat: { supportsStore: true },
      },
    ],
  },
});
assert.equal(JSON.stringify(catalog).includes("credential-secret"), false);

console.log("credential gateway image metadata boundary: ok");
