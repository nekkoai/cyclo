// Runs inside the built gateway image with --network none. Loopback is enough
// to exercise the real proxy without letting the fixture contact the Internet.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";

const authPath = process.env.CYCLO_GATEWAY_AUTH_JSON;
const modelsPath = process.env.CYCLO_GATEWAY_MODELS_JSON;
const clientsPath = process.env.CYCLO_GATEWAY_CLIENTS_JSON;
const usagePath = process.env.CYCLO_GATEWAY_USAGE_JSONL;
const gatewayTokenFile = process.env.CYCLO_GATEWAY_TOKEN_FILE;
assert.ok(authPath);
assert.ok(modelsPath);
assert.ok(clientsPath);
assert.ok(usagePath);
assert.ok(gatewayTokenFile);
const gatewayToken = (await readFile(gatewayTokenFile, "utf8")).trim();
assert.ok(gatewayToken);

const teamToken = "image-smoke-team-token";
const oldRotationKey = "credential-rotation-old-secret";
const newRotationKey = "credential-rotation-new-secret";
const accountId = "derived-openai-account-secret";
const codexPayload = Buffer.from(JSON.stringify({
  "https://api.openai.com/auth": { chatgpt_account_id: accountId },
})).toString("base64url");
const codexAccess = `header.${codexPayload}.signature`;
const upstreamRequests = [];
let slowRequestStarted;
let slowResponseClosed;
const slowStarted = new Promise((resolve) => { slowRequestStarted = resolve; });
const slowClosed = new Promise((resolve) => { slowResponseClosed = resolve; });

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}

function close(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function requestBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

const upstream = createServer(async (request, response) => {
  try {
    const body = await requestBody(request);
    const authorization = request.headers.authorization ?? "";
    upstreamRequests.push({
      authorization,
      accountId: request.headers["chatgpt-account-id"],
      body: body.toString("utf8"),
      requestId: request.headers["x-client-request-id"],
    });

    if (request.headers["x-client-request-id"] === "echo-credential") {
      response.writeHead(200, {
        "content-type": "text/plain",
        "x-reflected-credential": authorization,
      });
      const reflected = `before ${authorization} after`;
      const midpoint = Math.floor(reflected.length / 2);
      response.write(reflected.slice(0, midpoint));
      setImmediate(() => response.end(reflected.slice(midpoint)));
      return;
    }

    if (request.headers["x-client-request-id"] === "echo-account-id") {
      const reflected = request.headers["chatgpt-account-id"] ?? "missing";
      response.writeHead(200, {
        "content-type": "text/plain",
        "x-reflected-account": reflected,
      });
      response.end(`account=${reflected}`);
      return;
    }

    if (request.headers["x-client-request-id"] === "slow-cancel") {
      slowRequestStarted();
      const timer = setTimeout(() => {
        if (!response.destroyed) response.end("too late");
      }, 2_000);
      timer.unref?.();
      response.once("close", () => {
        clearTimeout(timer);
        slowResponseClosed();
      });
      return;
    }

    response.writeHead(200, {
      "content-type": "application/json",
      authorization: "upstream-response-capability",
      "set-cookie": "upstream-session=secret",
      "x-api-key": "upstream-response-key",
    });
    response.end(JSON.stringify({
      id: "fake-response",
      usage: { prompt_tokens: 2, completion_tokens: 3 },
    }));
  } catch (error) {
    response.writeHead(500, { "content-type": "text/plain" });
    response.end("fake upstream failure\n");
  }
});

const upstreamPort = await listen(upstream);
const upstreamBase = `http://127.0.0.1:${upstreamPort}/v1`;
const credentials = {
  account: { type: "api_key", provider: "custom", key: "credential-secret" },
  _legacy: { type: "api_key", provider: "custom", key: "credential-legacy-secret" },
  "rotation-a": { type: "api_key", provider: "custom", key: oldRotationKey },
  "codex-reflection": {
    type: "oauth",
    provider: "openai-codex",
    access: codexAccess,
    refresh: "unused-refresh-secret",
    expires: Date.now() + 3_600_000,
  },
  constructor: { type: "api_key", provider: "custom", key: "must-not-route" },
  prototype: { type: "api_key", provider: "custom", key: "must-not-route" },
};

await writeFile(authPath, `${JSON.stringify(credentials)}\n`, { encoding: "utf8", mode: 0o600 });
await writeFile(
  modelsPath,
  `${JSON.stringify({
    providers: {
      custom: {
        baseUrl: upstreamBase,
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
      "openai-codex": {
        baseUrl: upstreamBase,
        api: "openai-responses",
        models: [{ id: "safe-model", input: ["text"] }],
      },
    },
  })}\n`,
  { encoding: "utf8", mode: 0o600 },
);
await writeFile(
  clientsPath,
  `${JSON.stringify({
    version: 1,
    clients: [
      {
        client_id: "hostile-team",
        team_id: "hostile-team",
        binding_generation: "image-smoke-generation",
        token_sha256: createHash("sha256").update(teamToken).digest("hex"),
        providers: ["account", "_legacy", "rotation-a", "codex-reflection"],
        models: [
          "account/safe-model",
          "_legacy/safe-model",
          "rotation-a/safe-model",
          "codex-reflection/safe-model",
        ],
        expires_at: null,
        enabled: true,
        revoked: false,
      },
    ],
  })}\n`,
  { encoding: "utf8", mode: 0o600 },
);
await writeFile(usagePath, "", { encoding: "utf8", mode: 0o600 });

const { safeModelFields } = await import("/app/safe-model-fields.mjs");
const { sanitizeModel } = await import("/app/model-metadata.mjs");
const { parseArgs: parseLoginArgs } = await import("/app/login.mjs");
assert.equal(sanitizeModel({ id: "direct-smoke" }).id, "direct-smoke");
assert.equal(safeModelFields.schemaVersion, 1);
assert.equal(parseLoginArgs(["openai", "--as", "openai-work"]).account, "openai-work");
assert.throws(() => parseLoginArgs(["openai", "--as", "Bad.Name"]), /account name/);

const { createGatewayServer, providerCatalog } = await import("/app/server.mjs");
const catalog = providerCatalog();
assert.deepEqual(Object.keys(catalog).sort(), [
  "_legacy",
  "account",
  "codex-reflection",
  "rotation-a",
]);
assert.equal(JSON.stringify(catalog).includes("credential-secret"), false);

const gateway = createGatewayServer();
const gatewayPort = await listen(gateway);
const gatewayBase = `http://127.0.0.1:${gatewayPort}`;

async function gatewayRequest(path, {
  headers = {},
  body = { model: "safe-model", messages: [{ role: "user", content: "smoke" }] },
} = {}) {
  const response = await fetch(`${gatewayBase}${path}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
      ...headers,
    },
    body: JSON.stringify(body),
  });
  return { response, text: await response.text() };
}

try {
  const normal = await gatewayRequest("/p/account/chat/completions");
  assert.equal(normal.response.status, 200);
  assert.equal(upstreamRequests.at(-1).authorization, "Bearer credential-secret");
  assert.equal(normal.response.headers.has("authorization"), false);
  assert.equal(normal.response.headers.has("set-cookie"), false);
  assert.equal(normal.response.headers.has("x-api-key"), false);

  const reflected = await gatewayRequest("/p/account/chat/completions", {
    headers: { "x-client-request-id": "echo-credential" },
  });
  assert.equal(reflected.response.status, 200);
  assert.equal(reflected.response.headers.has("x-reflected-credential"), false);
  assert.equal(reflected.text, "before [REDACTED] after");
  assert.equal(reflected.text.includes("credential-secret"), false);

  const legacy = await gatewayRequest("/p/_legacy/chat/completions");
  assert.equal(legacy.response.status, 200);
  assert.equal(upstreamRequests.at(-1).authorization, "Bearer credential-legacy-secret");

  const codex = await gatewayRequest("/p/codex-reflection/responses", {
    headers: { "x-client-request-id": "echo-account-id" },
    body: { model: "safe-model", input: "smoke" },
  });
  assert.equal(codex.response.status, 200);
  assert.equal(codex.response.headers.has("x-reflected-account"), false);
  assert.equal(codex.text, "account=[REDACTED]");

  const rotationBody = JSON.stringify({
    model: "safe-model",
    messages: [{ role: "user", content: "rotate" }],
  });
  const midpoint = Math.floor(rotationBody.length / 2);
  let finishUpload;
  const rotatedResponse = new Promise((resolve, reject) => {
    const request = httpRequest({
      host: "127.0.0.1",
      port: gatewayPort,
      path: "/p/rotation-a/chat/completions",
      method: "POST",
      headers: {
        authorization: `Bearer ${teamToken}`,
        "content-type": "application/json",
        "content-length": Buffer.byteLength(rotationBody),
      },
    }, async (response) => {
      try {
        resolve({ status: response.statusCode, body: (await requestBody(response)).toString("utf8") });
      } catch (error) {
        reject(error);
      }
    });
    request.once("error", reject);
    request.write(rotationBody.slice(0, midpoint));
    finishUpload = () => request.end(rotationBody.slice(midpoint));
  });
  credentials["rotation-a"].key = newRotationKey;
  await writeFile(authPath, `${JSON.stringify(credentials)}\n`, { encoding: "utf8", mode: 0o600 });
  finishUpload();
  assert.equal((await rotatedResponse).status, 200);
  assert.equal(upstreamRequests.at(-1).authorization, `Bearer ${newRotationKey}`);

  const controller = new AbortController();
  const cancelled = fetch(`${gatewayBase}/p/account/chat/completions`, {
    method: "POST",
    signal: controller.signal,
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
      "x-client-request-id": "slow-cancel",
    },
    body: JSON.stringify({ model: "safe-model", messages: [] }),
  });
  await slowStarted;
  controller.abort();
  await assert.rejects(cancelled, /abort/i);
  await Promise.race([
    slowClosed,
    new Promise((_, reject) => setTimeout(() => reject(new Error("upstream was not cancelled")), 1_000)),
  ]);

  let usage = [];
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const text = await readFile(usagePath, "utf8");
    usage = JSON.parse(`[${text.trim().split("\n").filter(Boolean).join(",")}]`);
    if (usage.some((record) => record.provider === "account" && record.status === 499)) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(usage.some((record) => record.provider === "account" && record.status === 200));
  assert.ok(usage.some((record) => record.provider === "account" && record.status === 499));

  console.log("credential gateway image security boundary: ok");
} finally {
  await close(gateway);
  await close(upstream);
}
