// CYCLO credential gateway.
//
// Agent containers talk to this server instead of LLM providers. Each request
// carries the per-run gateway token; the gateway swaps it for the real
// provider credential and forwards the request unchanged. Real credentials
// never enter agent containers.
//
// Credentials live in the gateway's OWN private store (a writable volume),
// which nothing else writes. For OAuth providers this is the gateway's own
// session (its own refresh-token chain, provisioned once via `login.mjs`) --
// independent of the host pi's session, so refresh-token rotation never races
// a second writer. API-key providers are provisioned into the same store.
//
// Provider *configuration* (baseUrls, dialects for custom providers) is read
// separately and read-only from a projected models.json plus pi-ai's built-in
// registry. Config is shared seamlessly; the mutable secret store is not.

import { createServer } from "node:http";
import { createHash, timingSafeEqual } from "node:crypto";
import { pathToFileURL } from "node:url";
import { getModels, getProviders } from "@earendil-works/pi-ai";
import { getOAuthProvider } from "@earendil-works/pi-ai/oauth";
import { readJson, withFileLock, writeJsonAtomic } from "./store.mjs";
import {
  MAX_USAGE_CAPTURE_BYTES,
  aggregateUsageFile,
  appendAuditRecord,
  usageFromCapture,
} from "./usage.mjs";
import {
  clientPrincipalFromRegistryEntry,
  filterModelsForPrincipal,
  isKnownInferenceEndpoint,
  modelFromInferenceRequest,
  principalAllowsModel,
} from "./policy.mjs";

const PORT = Number(process.env.CYCLO_GATEWAY_PORT ?? 8787);
const TOKEN = process.env.CYCLO_GATEWAY_TOKEN;
// The gateway's own private credential store (writable volume, sole writer).
const AUTH_JSON_PATH = process.env.CYCLO_GATEWAY_AUTH_JSON ?? "/var/lib/cyclo-gateway/auth.json";
// Read-only provider config projected from the host pi (baseUrls, dialects).
const MODELS_JSON_PATH = process.env.CYCLO_GATEWAY_MODELS_JSON ?? "/run/pi/models.json";
// Optional, read-only, hash-only project capability registry.
const CLIENTS_JSON_PATH = process.env.CYCLO_GATEWAY_CLIENTS_JSON;
const USAGE_JSONL_PATH = process.env.CYCLO_GATEWAY_USAGE_JSONL ?? "/var/lib/cyclo-gateway/usage.jsonl";
const UPSTREAM_TIMEOUT_MS = 600_000;
const MAX_REQUEST_BODY = 64 * 1024 * 1024;
const OAUTH_REFRESH_SKEW_MS = 60_000;

const SAFE_COST_FIELDS = new Set(["input", "output", "cacheRead", "cacheWrite"]);
const SAFE_INPUT_TYPES = new Set(["text", "image"]);
const SAFE_COMPAT_BOOLEAN_FIELDS = new Set([
  "requiresAssistantAfterToolResult",
  "requiresReasoningContentOnAssistantMessages",
  "requiresThinkingAsText",
  "requiresToolResultName",
  "sendSessionAffinityHeaders",
  "sendSessionIdHeader",
  "supportsDeveloperRole",
  "supportsEagerToolInputStreaming",
  "supportsLongCacheRetention",
  "supportsReasoningEffort",
  "supportsStore",
  "supportsStrictMode",
  "supportsUsageInStreaming",
  "zaiToolStream",
]);
const SAFE_MAX_TOKENS_FIELDS = new Set(["max_completion_tokens", "max_tokens"]);
const SAFE_THINKING_FORMATS = new Set([
  "openai",
  "openrouter",
  "deepseek",
  "zai",
  "qwen",
  "qwen-chat-template",
]);
const SAFE_THINKING_LEVELS = new Set(["off", "minimal", "low", "medium", "high", "xhigh"]);

// How an api_key credential is presented upstream, per pi-ai api dialect.
const AUTH_HEADER_BY_API = {
  "anthropic-messages": (h, key) => (h["x-api-key"] = key),
  "google-generative-ai": (h, key) => (h["x-goog-api-key"] = key),
};
const DEFAULT_AUTH_HEADER = (h, key) => (h["authorization"] = `Bearer ${key}`);

// OAuth providers send the access token as a bearer and, for some providers,
// extra identity headers derived from the credential. This is the only place
// per-provider HTTP quirks live; the OAuth flows themselves come from pi-ai.
//
// For anthropic and github-copilot the agent's own pi already emits the
// provider-specific request body + identity headers (Claude-Code system prompt,
// copilot editor headers) because the projected token puts pi in the right
// mode; the gateway only swaps the bearer for the real credential.
const OAUTH_DECORATORS = {
  "openai-codex": (h, cred) => {
    h["authorization"] = `Bearer ${cred.access}`;
    const accountId = cred.accountId ?? accountIdFromAccessToken(cred.access);
    if (accountId) h["chatgpt-account-id"] = accountId;
    h["originator"] = "pi";
  },
  anthropic: (h, cred) => {
    h["authorization"] = `Bearer ${cred.access}`;
  },
  "github-copilot": (h, cred) => {
    h["authorization"] = `Bearer ${cred.access}`;
    if (!h["copilot-integration-id"]) h["copilot-integration-id"] = "vscode-chat";
  },
};
const DEFAULT_OAUTH_DECORATOR = (h, cred) => (h["authorization"] = `Bearer ${cred.access}`);

// Request headers forwarded upstream. Everything else (including the client's
// Authorization / x-api-key) is dropped and replaced.
const FORWARDED_REQUEST_HEADERS = new Set([
  "accept",
  "content-type",
  "anthropic-version",
  "anthropic-beta",
  "anthropic-dangerous-direct-browser-access",
  "x-app",
  "openai-beta",
  "session_id",
  "x-client-request-id",
  "user-agent",
  // github-copilot editor identity + per-request hints (pi sets these)
  "copilot-integration-id",
  "editor-version",
  "editor-plugin-version",
  "x-initiator",
  "openai-intent",
  "copilot-vision-request",
]);

// Response headers never relayed back. content-encoding/content-length are
// dropped because fetch() transparently decompresses; node re-chunks.
const DROPPED_RESPONSE_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "content-encoding",
  "content-length",
]);

if (!TOKEN) {
  console.error("error: CYCLO_GATEWAY_TOKEN is required");
  process.exit(2);
}

// Re-read the store and config on every call so provisioning and provider
// edits take effect without restarting the gateway.
function objectDocument(value, label) {
  if (value === null) return {};
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value;
}

function credentialStore() {
  return objectDocument(readJson(AUTH_JSON_PATH), `credential store ${AUTH_JSON_PATH}`);
}

function customProviderConfig() {
  const document = objectDocument(readJson(MODELS_JSON_PATH), `provider config ${MODELS_JSON_PATH}`);
  if (document.providers === undefined) return {};
  return objectDocument(document.providers, `providers in ${MODELS_JSON_PATH}`);
}

function customBaseUrl(name, value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch (exc) {
    throw new Error(`custom provider ${name} has an invalid baseUrl: ${exc.message}`);
  }
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error(`custom provider ${name} baseUrl must use http or https`);
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(
      `custom provider ${name} baseUrl must not contain credentials, query parameters, or a fragment`,
    );
  }
  return parsed.toString();
}

function discoverProviders() {
  const auth = credentialStore();
  const custom = customProviderConfig();
  const builtin = new Set(getProviders());
  const table = {};
  for (const [name, cred] of Object.entries(auth)) {
    // `name` is the ACCOUNT (store key); the provider TYPE is cred.provider,
    // falling back to the key itself for stores written before multi-account.
    // This lets several accounts share one provider type (e.g. two anthropic
    // logins under different names).
    if (!cred || typeof cred !== "object" || !["api_key", "oauth"].includes(cred.type)) continue;
    const providerType = typeof cred.provider === "string" && cred.provider ? cred.provider : name;
    const configured = custom[providerType];
    if (configured && typeof configured === "object" && typeof configured.baseUrl === "string") {
      table[name] = {
        provider: providerType,
        baseUrl: customBaseUrl(providerType, configured.baseUrl),
        api: typeof configured.api === "string" && configured.api
          ? configured.api
          : "openai-completions",
        credType: cred.type,
        models: Array.isArray(configured.models) ? configured.models : [],
      };
      continue;
    }
    if (!builtin.has(providerType)) continue;
    const models = getModels(providerType);
    if (!models.length || typeof models[0].baseUrl !== "string") continue;
    table[name] = { provider: providerType, baseUrl: models[0].baseUrl, api: models[0].api, credType: cred.type, models };
  }
  return table;
}

function sanitizeModel(model) {
  if (!model || typeof model !== "object" || typeof model.id !== "string" || !model.id) return null;
  const clean = { id: model.id };
  for (const key of ["name", "provider", "api"]) {
    if (typeof model[key] === "string" && model[key]) clean[key] = model[key];
  }
  if (typeof model.reasoning === "boolean") clean.reasoning = model.reasoning;
  if (Array.isArray(model.input)) {
    clean.input = model.input.filter((value) => typeof value === "string" && SAFE_INPUT_TYPES.has(value));
  }
  for (const key of ["contextWindow", "maxTokens"]) {
    if (Number.isSafeInteger(model[key]) && model[key] > 0) clean[key] = model[key];
  }
  if (model.cost && typeof model.cost === "object") {
    const cost = {};
    for (const key of SAFE_COST_FIELDS) {
      if (typeof model.cost[key] === "number" && Number.isFinite(model.cost[key]) && model.cost[key] >= 0) {
        cost[key] = model.cost[key];
      }
    }
    if (Object.keys(cost).length) clean.cost = cost;
  }
  if (model.compat && typeof model.compat === "object") {
    const compat = {};
    for (const key of SAFE_COMPAT_BOOLEAN_FIELDS) {
      if (typeof model.compat[key] === "boolean") compat[key] = model.compat[key];
    }
    if (SAFE_MAX_TOKENS_FIELDS.has(model.compat.maxTokensField)) {
      compat.maxTokensField = model.compat.maxTokensField;
    }
    if (SAFE_THINKING_FORMATS.has(model.compat.thinkingFormat)) {
      compat.thinkingFormat = model.compat.thinkingFormat;
    }
    if (model.compat.cacheControlFormat === "anthropic") compat.cacheControlFormat = "anthropic";
    if (Object.keys(compat).length) clean.compat = compat;
  }
  if (model.thinkingLevelMap && typeof model.thinkingLevelMap === "object") {
    const thinkingLevelMap = {};
    for (const key of SAFE_THINKING_LEVELS) {
      const value = model.thinkingLevelMap[key];
      if (typeof value === "string" || value === null) {
        thinkingLevelMap[key] = value;
      }
    }
    if (Object.keys(thinkingLevelMap).length) clean.thinkingLevelMap = thinkingLevelMap;
  }
  return clean;
}

// The provider catalog the runner needs to build each agent container's
// projected models.json: provider name -> api dialect + model list. baseUrls
// are not returned; the runner rewrites them to point back at this gateway.
function providerCatalog(principal = { kind: "admin", providers: ["*"] }) {
  const catalog = {};
  for (const [name, p] of Object.entries(discoverProviders())) {
    if (!principalAllows(principal, name)) continue;
    catalog[name] = {
      api: p.api,
      models: filterModelsForPrincipal(
        principal,
        name,
        (p.models ?? []).map(sanitizeModel),
      ),
    };
  }
  return catalog;
}

function resolveApiKey(name, provider) {
  const cred = credentialStore()[name];
  if (cred?.type === "api_key" && typeof cred.key === "string") return cred.key;
  throw new Error(`no api_key credential for provider ${name}`);
}

function accountIdFromAccessToken(access) {
  try {
    const [, payload] = String(access).split(".");
    const json = Buffer.from(payload.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
    const claim = JSON.parse(json)["https://api.openai.com/auth"];
    const id = claim?.chatgpt_account_id;
    return typeof id === "string" && id ? id : null;
  } catch {
    return null;
  }
}

function notExpired(cred) {
  return cred && typeof cred.access === "string" && Date.now() + OAUTH_REFRESH_SKEW_MS < (Number(cred.expires) || 0);
}

// Serialize refreshes within this process per provider so concurrent agent
// requests don't trigger parallel refreshes of the same credential.
const refreshInFlight = new Map();

// Return a non-expired OAuth credential for `name`, refreshing if needed.
// Correct across multiple gateway containers sharing one store volume: the
// refresh runs under a cross-process file lock with a double-check, so only the
// first refresher rotates the token and later ones reuse the fresh result
// rather than refreshing a refresh-token that was just rotated out from under
// them. In-process requests are additionally deduped by `refreshInFlight`.
async function freshOAuthCredential(name) {
  const cred = credentialStore()[name];
  if (!cred || cred.type !== "oauth" || typeof cred.access !== "string") {
    throw new Error(`no oauth credential for provider ${name}`);
  }
  if (notExpired(cred)) return cred;

  if (!refreshInFlight.has(name)) {
    refreshInFlight.set(
      name,
      withFileLock(AUTH_JSON_PATH, async () => {
        const store = credentialStore();
        const current = store[name];
        if (notExpired(current)) return current; // another writer already refreshed
        const oauth = getOAuthProvider(current?.provider ?? cred.provider ?? name);
        if (!oauth) throw new Error(`pi-ai has no oauth provider for ${name}`);
        const refreshed = await oauth.refreshToken(current ?? cred);
        const accountId = refreshed.accountId ?? current?.accountId ?? accountIdFromAccessToken(refreshed.access);
        store[name] = { ...current, ...refreshed, type: "oauth", ...(accountId ? { accountId } : {}) };
        writeJsonAtomic(AUTH_JSON_PATH, store);
        return store[name];
      }).finally(() => refreshInFlight.delete(name)),
    );
  }
  return refreshInFlight.get(name);
}

function presentedToken(req) {
  const auth = req.headers["authorization"];
  const presented = typeof auth === "string" && auth.startsWith("Bearer ") ? auth.slice(7) : req.headers["x-api-key"];
  return typeof presented === "string" && presented.length > 0 ? presented : null;
}

function digestToken(token) {
  return createHash("sha256").update(token).digest();
}

function hashMatches(digest, hex) {
  if (typeof hex !== "string" || !/^[a-f0-9]{64}$/i.test(hex)) return false;
  const expected = Buffer.from(hex, "hex");
  return expected.length === digest.length && timingSafeEqual(digest, expected);
}

function registryClients() {
  if (!CLIENTS_JSON_PATH) return [];
  const registry = readJson(CLIENTS_JSON_PATH);
  if (registry?.version !== 1 || !Array.isArray(registry.clients)) return [];
  return registry.clients;
}

function expiryMillis(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value < 10_000_000_000 ? value * 1000 : value;
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : Number.NaN;
  }
  return Number.NaN;
}

function authenticate(req, now = Date.now()) {
  const presented = presentedToken(req);
  if (!presented) return null;
  const digest = digestToken(presented);
  if (timingSafeEqual(digest, digestToken(TOKEN))) {
    return {
      kind: "admin",
      client_id: "admin",
      team_id: "admin",
      binding_generation: null,
      providers: ["*"],
      models: ["*"],
    };
  }
  for (const entry of registryClients()) {
    if (!hashMatches(digest, entry?.token_sha256)) continue;
    const expiresAt = expiryMillis(entry.expires_at);
    if (entry.enabled === false || entry.revoked === true) return null;
    if (expiresAt !== null && (!Number.isFinite(expiresAt) || now >= expiresAt)) return null;
    if (typeof entry.client_id !== "string" || !entry.client_id) return null;
    return clientPrincipalFromRegistryEntry(entry);
  }
  return null;
}

function principalAllows(principal, provider) {
  return (
    principal?.kind === "admin" ||
    principal?.providers?.includes("*") ||
    principal?.providers?.includes(provider)
  );
}

function tokenOk(req) {
  return authenticate(req) !== null;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_REQUEST_BODY) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function sendPlain(res, status, text) {
  const body = Buffer.from(text, "utf8");
  res.writeHead(status, {
    "content-type": "text/plain; charset=utf-8",
    "content-length": String(body.length),
  });
  res.end(body);
}

// GitHub Copilot encodes its API host inside the token (proxy-ep=...), so the
// upstream base URL is per-credential rather than the static registry value.
export function copilotBaseUrl(token) {
  const match = String(token || "").match(/proxy-ep=([^;]+)/);
  const host = match ? match[1].replace(/^proxy\./, "api.") : "api.individual.githubcopilot.com";
  return `https://${host}`;
}

// Build the upstream request: forwarded headers + injected credential, and the
// effective base URL (which a provider may override from its credential).
async function resolveUpstream(req, name, provider) {
  const headers = { "accept-encoding": "identity" };
  for (const [key, value] of Object.entries(req.headers)) {
    if (FORWARDED_REQUEST_HEADERS.has(key.toLowerCase()) && typeof value === "string") {
      headers[key] = value;
    }
  }
  // Per-provider HTTP quirks key off the provider TYPE, not the account name,
  // so an account like "claude-work" still gets the anthropic decorator.
  const providerType = provider.provider ?? name;
  let baseUrl = provider.baseUrl;
  if (provider.credType === "oauth") {
    const cred = await freshOAuthCredential(name);
    (OAUTH_DECORATORS[providerType] ?? DEFAULT_OAUTH_DECORATOR)(headers, cred);
    if (providerType === "github-copilot") baseUrl = copilotBaseUrl(cred.access);
  } else if (provider.credType !== "none") {
    (AUTH_HEADER_BY_API[provider.api] ?? DEFAULT_AUTH_HEADER)(headers, resolveApiKey(name, provider));
  }
  return { headers, baseUrl };
}

function auditProxy(principal, provider, model, status, started, requestBytes, responseBytes, usage = {}) {
  const record = {
    event: "proxy_request",
    timestamp: new Date().toISOString(),
    principal: principal.kind,
    client_id: principal.client_id,
    team_id: principal.team_id,
    binding_generation: principal.binding_generation ?? null,
    provider,
    model: model || "unknown",
    status,
    latency_ms: Math.max(0, Date.now() - started),
    request_bytes: requestBytes,
    response_bytes: responseBytes,
    input_tokens: Number(usage.input_tokens) || 0,
    output_tokens: Number(usage.output_tokens) || 0,
    cache_read_tokens: Number(usage.cache_read_tokens) || 0,
    cache_write_tokens: Number(usage.cache_write_tokens) || 0,
    usage_source: typeof usage.usage_source === "string" ? usage.usage_source : "unavailable",
    capture_truncated: usage.capture_truncated === true,
  };
  appendAuditRecord(USAGE_JSONL_PATH, record).catch((exc) => {
    console.error(`gateway usage write failed: ${exc.message}`);
  });
}

async function handleProxy(req, res, principal, name, restPath, search) {
  const started = Date.now();
  if (!isKnownInferenceEndpoint(req.method, restPath)) {
    sendPlain(res, 405, "only known POST inference endpoints are allowed\n");
    auditProxy(principal, name, "unknown", 405, started, 0, 0);
    return;
  }
  const provider = discoverProviders()[name];
  if (!provider) {
    sendPlain(res, 404, `unknown provider: ${name}\n`);
    auditProxy(principal, name, "unknown", 404, started, 0, 0);
    return;
  }
  let body;
  try {
    body = req.method === "GET" || req.method === "HEAD" ? undefined : await readBody(req);
  } catch (exc) {
    sendPlain(res, 413, `${exc.message}\n`);
    auditProxy(principal, name, "unknown", 413, started, 0, 0);
    return;
  }
  const requestBytes = body?.length ?? 0;
  const model = modelFromInferenceRequest(restPath, body);
  if (!model) {
    sendPlain(res, 400, "inference request does not identify a model\n");
    auditProxy(principal, name, "unknown", 400, started, requestBytes, 0);
    return;
  }
  if (!principalAllowsModel(principal, name, model)) {
    sendPlain(res, 403, "model outside client scope\n");
    auditProxy(principal, name, model, 403, started, requestBytes, 0);
    return;
  }
  let headers;
  let baseUrl;
  try {
    ({ headers, baseUrl } = await resolveUpstream(req, name, provider));
  } catch (exc) {
    sendPlain(res, 502, `gateway credential error: ${exc.message}\n`);
    auditProxy(principal, name, model, 502, started, requestBytes, 0);
    return;
  }
  const base = baseUrl.replace(/\/+$/, "");
  const url = new URL(base + restPath + search);
  let upstream;
  try {
    upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (exc) {
    sendPlain(res, 502, `upstream error: ${exc.message}\n`);
    auditProxy(principal, name, model, 502, started, requestBytes, 0);
    return;
  }
  const responseHeaders = {};
  for (const [key, value] of upstream.headers.entries()) {
    if (!DROPPED_RESPONSE_HEADERS.has(key.toLowerCase())) {
      responseHeaders[key] = value;
    }
  }
  res.writeHead(upstream.status, responseHeaders);
  let responseBytes = 0;
  let captureBytes = 0;
  const capture = [];
  if (upstream.body) {
    try {
      for await (const chunk of upstream.body) {
        res.write(chunk);
        const bytes = Buffer.from(chunk);
        responseBytes += bytes.length;
        if (captureBytes < MAX_USAGE_CAPTURE_BYTES) {
          const remaining = MAX_USAGE_CAPTURE_BYTES - captureBytes;
          const part = bytes.length <= remaining ? bytes : bytes.subarray(0, remaining);
          capture.push(part);
          captureBytes += part.length;
        }
      }
    } catch {
      const usage = usageFromCapture(
        Buffer.concat(capture),
        upstream.headers.get("content-type") ?? "",
      );
      usage.capture_truncated = true;
      auditProxy(
        principal,
        name,
        model,
        upstream.status,
        started,
        requestBytes,
        responseBytes,
        usage,
      );
      res.destroy();
      return;
    }
  }
  res.end();
  const usage = usageFromCapture(
    Buffer.concat(capture),
    upstream.headers.get("content-type") ?? "",
  );
  usage.capture_truncated = responseBytes > captureBytes;
  auditProxy(
    principal,
    name,
    model,
    upstream.status,
    started,
    requestBytes,
    responseBytes,
    usage,
  );
  console.log(`${req.method} ${name}${restPath} -> ${upstream.status} (${Date.now() - started}ms)`);
}

function createGatewayServer() {
  return createServer((req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://gateway");
    if (req.method === "GET" && url.pathname === "/health") {
      sendPlain(res, 200, "ok\n");
      return;
    }
    if (req.method === "GET" && url.pathname === "/providers") {
      const principal = authenticate(req);
      if (!principal) {
        sendPlain(res, 401, "unauthorized\n");
        return;
      }
      const body = Buffer.from(JSON.stringify(providerCatalog(principal)), "utf8");
      res.writeHead(200, { "content-type": "application/json", "content-length": String(body.length) });
      res.end(body);
      return;
    }
    if (req.method === "GET" && url.pathname === "/usage") {
      const principal = authenticate(req);
      if (!principal) {
        sendPlain(res, 401, "unauthorized\n");
        return;
      }
      if (principal.kind !== "admin") {
        sendPlain(res, 403, "forbidden\n");
        return;
      }
      aggregateUsageFile(USAGE_JSONL_PATH).then(
        (usage) => {
          const body = Buffer.from(JSON.stringify(usage), "utf8");
          res.writeHead(200, {
            "content-type": "application/json",
            "content-length": String(body.length),
          });
          res.end(body);
        },
        (exc) => sendPlain(res, 500, `usage read failed: ${exc.message}\n`),
      );
      return;
    }
    const match = url.pathname.match(/^\/p\/([a-z0-9_-]+)(\/.*)?$/);
    if (!match) {
      sendPlain(res, 404, "not found\n");
      return;
    }
    const principal = authenticate(req);
    if (!principal) {
      sendPlain(res, 401, "unauthorized\n");
      return;
    }
    if (!principalAllows(principal, match[1])) {
      const started = Date.now();
      sendPlain(res, 403, "provider outside client scope\n");
      auditProxy(principal, match[1], "unknown", 403, started, 0, 0);
      return;
    }
      handleProxy(req, res, principal, match[1], match[2] ?? "/", url.search).catch((exc) => {
        if (!res.headersSent) {
          sendPlain(res, 502, `gateway error: ${exc.message}\n`);
        } else {
          res.destroy();
        }
      });
    } catch (exc) {
      console.error(`gateway configuration read failed: ${exc.message}`);
      if (!res.headersSent) sendPlain(res, 500, "gateway configuration error\n");
      else res.destroy();
    }
  });
}

function main() {
  const server = createGatewayServer();
  server.listen(PORT, "0.0.0.0", () => {
    const table = discoverProviders();
    console.log(`cyclo-gateway listening on :${PORT}`);
    for (const [name, p] of Object.entries(table)) {
      console.log(`provider ${name}: ${p.baseUrl} (${p.api}, ${p.credType})`);
    }
    if (!Object.keys(table).length) console.log("providers: (none discovered)");
  });
  process.on("SIGTERM", () => server.close(() => process.exit(0)));
  process.on("SIGINT", () => server.close(() => process.exit(0)));
}

// Start the server only when run directly, not when imported by tests.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}

export {
  freshOAuthCredential,
  accountIdFromAccessToken,
  discoverProviders,
  providerCatalog,
  customBaseUrl,
  sanitizeModel,
  authenticate,
  principalAllows,
  principalAllowsModel,
  isKnownInferenceEndpoint,
  tokenOk,
  createGatewayServer,
};
