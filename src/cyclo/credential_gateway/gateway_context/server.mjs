// CYCLO credential gateway.
//
// Cyclo's provider runtime sends concrete LLM requests here on behalf of agent
// containers. Each request retains the per-run bearer; the gateway swaps it
// for the real provider credential and forwards the request unchanged. Real
// credentials never enter either agent or provider-component containers.
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
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { getOAuthProvider } from "@earendil-works/pi-ai/oauth";
import { getBuiltinModels, getBuiltinProviders } from "./pi-registry.mjs";
import { sanitizeModel } from "./model-metadata.mjs";
import {
  forwardedRequestHeaders,
  prepareRequestBody,
} from "./request-body.mjs";
import {
  createResponseSecretRedactor,
  filterResponseHeaders,
} from "./response-redaction.mjs";
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
const TOKEN_FILE = process.env.CYCLO_GATEWAY_TOKEN_FILE
  ?? "/run/secrets/cyclo-gateway-admin-token";
// The gateway's own private credential store (writable volume, sole writer).
const AUTH_JSON_PATH = process.env.CYCLO_GATEWAY_AUTH_JSON ?? "/var/lib/cyclo-gateway/auth.json";
// Read-only provider config projected from the host pi (baseUrls, dialects).
const MODELS_JSON_PATH = process.env.CYCLO_GATEWAY_MODELS_JSON ?? "/run/pi/models.json";
// Optional, read-only, hash-only project capability registry.
const CLIENTS_JSON_PATH = process.env.CYCLO_GATEWAY_CLIENTS_JSON;
const USAGE_JSONL_PATH = process.env.CYCLO_GATEWAY_USAGE_JSONL ?? "/var/lib/cyclo-gateway/usage.jsonl";
const UPSTREAM_TIMEOUT_MS = 600_000;
const MAX_REQUEST_BODY = 16 * 1024 * 1024;
const OAUTH_REFRESH_SKEW_MS = 60_000;
const ROUTE_NAME = /^[a-z0-9_-]+$/u;
const RESERVED_ROUTE_NAMES = new Set(["__proto__", "constructor", "gateway", "prototype"]);
const ACCOUNT_CREDENTIAL = Symbol("accountCredential");
const ACCOUNT_IDENTITY = Symbol("accountIdentity");

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

// Response headers never relayed back. content-encoding/content-length are
// dropped because fetch() transparently decompresses; node re-chunks.
const DROPPED_RESPONSE_HEADERS = new Set([
  "authorization",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "set-cookie",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "content-encoding",
  "content-length",
  "x-api-key",
]);

let TOKEN;
try {
  TOKEN = readFileSync(TOKEN_FILE, "utf8").trim();
} catch (error) {
  const cause = error.code ?? "read failed";
  console.error(`error: cannot read gateway administrator token file ${TOKEN_FILE}: ${cause}`);
  process.exit(2);
}
if (!TOKEN || /\s/u.test(TOKEN)) {
  console.error(`error: gateway administrator token file is malformed: ${TOKEN_FILE}`);
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

function validRouteName(value) {
  return typeof value === "string" && ROUTE_NAME.test(value) && !RESERVED_ROUTE_NAMES.has(value);
}

function accountIdentity(credential, provider, api, baseUrl) {
  const secret = credential.type === "api_key" ? credential.key : credential.access;
  return createHash("sha256").update(JSON.stringify({
    provider,
    api,
    baseUrl,
    credentialType: credential.type,
    secret,
  })).digest("hex");
}

function accountRecord({ name, provider, baseUrl, api, credential, models }) {
  const record = {
    kind: "gateway",
    provider,
    baseUrl,
    api,
    credType: credential.type,
    models,
  };
  Object.defineProperties(record, {
    [ACCOUNT_CREDENTIAL]: { value: credential },
    [ACCOUNT_IDENTITY]: {
      value: accountIdentity(credential, provider, api, baseUrl),
    },
  });
  return Object.freeze(record);
}

function discoverProviders() {
  const auth = credentialStore();
  const custom = customProviderConfig();
  const builtin = new Set(getBuiltinProviders());
  const table = Object.create(null);
  for (const [name, cred] of Object.entries(auth)) {
    // `name` is the ACCOUNT (store key); the provider TYPE is cred.provider,
    // falling back to the key itself for stores written before multi-account.
    // This lets several accounts share one provider type (e.g. two anthropic
    // logins under different names).
    if (!validRouteName(name)) continue;
    if (!cred || typeof cred !== "object" || Array.isArray(cred) || !["api_key", "oauth"].includes(cred.type)) continue;
    const providerType = typeof cred.provider === "string" && cred.provider ? cred.provider : name;
    if (!validRouteName(providerType)) continue;
    const configured = custom[providerType];
    if (configured && typeof configured === "object" && typeof configured.baseUrl === "string") {
      table[name] = accountRecord({
        name,
        provider: providerType,
        baseUrl: customBaseUrl(providerType, configured.baseUrl),
        api: typeof configured.api === "string" && configured.api
          ? configured.api
          : "openai-completions",
        credential: cred,
        models: Array.isArray(configured.models) ? configured.models : [],
      });
      continue;
    }
    if (!builtin.has(providerType)) continue;
    const models = getBuiltinModels(providerType);
    if (!models.length || typeof models[0].baseUrl !== "string") continue;
    table[name] = accountRecord({
      name,
      provider: providerType,
      baseUrl: models[0].baseUrl,
      api: models[0].api,
      credential: cred,
      models,
    });
  }
  return table;
}

// The concrete catalog consumed by the provider runtime: provider name -> API
// dialect + model list. baseUrls are never returned across this boundary.
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
  const cred = provider[ACCOUNT_CREDENTIAL];
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
function matchingOAuthCredential(credential, name, provider) {
  return credential
    && credential.type === "oauth"
    && (credential.provider ?? name) === provider
    && typeof credential.access === "string";
}

async function freshOAuthCredential(name, account) {
  const cred = account?.[ACCOUNT_CREDENTIAL] ?? credentialStore()[name];
  const provider = account?.provider ?? cred?.provider ?? name;
  if (!cred || cred.type !== "oauth" || typeof cred.access !== "string") {
    throw new Error(`no oauth credential for provider ${name}`);
  }
  if (notExpired(cred)) return cred;

  const refreshKey = `${name}\u0000${provider}`;
  if (!refreshInFlight.has(refreshKey)) {
    refreshInFlight.set(
      refreshKey,
      withFileLock(AUTH_JSON_PATH, async () => {
        const store = credentialStore();
        const current = store[name];
        if (!matchingOAuthCredential(current, name, provider)) {
          throw new Error(`oauth credential ${name} changed provider while refreshing`);
        }
        if (notExpired(current)) return current; // another writer already refreshed
        const oauth = getOAuthProvider(provider);
        if (!oauth) throw new Error(`pi-ai has no oauth provider for ${name}`);
        const refreshed = await oauth.refreshToken(current);
        const accountId = refreshed.accountId ?? current?.accountId ?? accountIdFromAccessToken(refreshed.access);
        store[name] = { ...current, ...refreshed, type: "oauth", ...(accountId ? { accountId } : {}) };
        writeJsonAtomic(AUTH_JSON_PATH, store);
        return store[name];
      }).finally(() => refreshInFlight.delete(refreshKey)),
    );
  }
  const refreshed = await refreshInFlight.get(refreshKey);
  if (!matchingOAuthCredential(refreshed, name, provider)) {
    throw new Error(`oauth credential ${name} changed provider while refreshing`);
  }
  return refreshed;
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

function writeWithBackpressure(res, chunk) {
  if (!chunk.length) return Promise.resolve();
  if (res.destroyed || res.writableEnded) {
    return Promise.reject(new Error("downstream response is closed"));
  }
  let accepted;
  try {
    accepted = res.write(chunk);
  } catch (exc) {
    return Promise.reject(exc);
  }
  if (accepted) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      res.off("drain", onDrain);
      res.off("close", onClose);
      res.off("error", onError);
    };
    const onDrain = () => {
      cleanup();
      resolve();
    };
    const onClose = () => {
      cleanup();
      reject(new Error("downstream response closed before drain"));
    };
    const onError = (error) => {
      cleanup();
      reject(error);
    };
    res.once("drain", onDrain);
    res.once("close", onClose);
    res.once("error", onError);
    if (res.destroyed || res.writableEnded) onClose();
  });
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
  const headers = forwardedRequestHeaders(req.headers);
  const sensitiveValues = [];
  // Per-provider HTTP quirks key off the provider TYPE, not the account name,
  // so an account like "claude-work" still gets the anthropic decorator.
  const providerType = provider.provider ?? name;
  let baseUrl = provider.baseUrl;
  if (provider.credType === "oauth") {
    const cred = await freshOAuthCredential(name, provider);
    (OAUTH_DECORATORS[providerType] ?? DEFAULT_OAUTH_DECORATOR)(headers, cred);
    sensitiveValues.push(cred.access, `Bearer ${cred.access}`);
    const sentAccountId = providerType === "openai-codex"
      ? cred.accountId ?? accountIdFromAccessToken(cred.access)
      : cred.accountId;
    if (typeof sentAccountId === "string" && sentAccountId) {
      sensitiveValues.push(sentAccountId);
    }
    if (providerType === "github-copilot") baseUrl = copilotBaseUrl(cred.access);
  } else if (provider.credType !== "none") {
    const key = resolveApiKey(name, provider);
    (AUTH_HEADER_BY_API[provider.api] ?? DEFAULT_AUTH_HEADER)(headers, key);
    sensitiveValues.push(key, `Bearer ${key}`);
  }
  return {
    headers,
    baseUrl,
    sensitiveValues,
    routeIdentity: provider[ACCOUNT_IDENTITY],
  };
}

function routeIsCurrent(name, selectedProvider, routeIdentity) {
  const current = discoverProviders()[name];
  return current
    && current.provider === selectedProvider.provider
    && current.api === selectedProvider.api
    && current.baseUrl === selectedProvider.baseUrl
    && current.credType === selectedProvider.credType
    && current[ACCOUNT_IDENTITY] === routeIdentity;
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
  let provider;
  try {
    provider = discoverProviders()[name];
  } catch (exc) {
    console.error(`gateway configuration read failed: ${exc.message}`);
    sendPlain(res, 500, "gateway configuration error\n");
    auditProxy(principal, name, "unknown", 500, started, 0, 0);
    return;
  }
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
  let policyBody;
  let upstreamBody;
  try {
    ({ policyBody, upstreamBody } = await prepareRequestBody(
      body,
      req.headers["content-encoding"],
      MAX_REQUEST_BODY,
    ));
  } catch (exc) {
    const status = Number.isInteger(exc?.statusCode) ? exc.statusCode : 400;
    sendPlain(res, status, `${exc.message}\n`);
    auditProxy(principal, name, "unknown", status, started, requestBytes, 0);
    return;
  }
  const model = modelFromInferenceRequest(restPath, policyBody);
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

  // Request parsing can be deliberately slow. Refresh the account snapshot
  // after all request-controlled work, then verify it again immediately before
  // dispatch so a concurrent login/config edit cannot mix a credential with an
  // obsolete upstream URL.
  try {
    provider = discoverProviders()[name];
  } catch (exc) {
    console.error(`gateway configuration refresh failed: ${exc.message}`);
    sendPlain(res, 500, "gateway configuration error\n");
    auditProxy(principal, name, model, 500, started, requestBytes, 0);
    return;
  }
  if (!provider) {
    sendPlain(res, 404, `unknown provider: ${name}\n`);
    auditProxy(principal, name, model, 404, started, requestBytes, 0);
    return;
  }

  const abort = new AbortController();
  let downstreamDisconnected = req.aborted || res.destroyed || res.writableEnded;
  const abortForClient = () => {
    downstreamDisconnected = true;
    if (!abort.signal.aborted) {
      abort.abort(new Error("downstream client disconnected"));
    }
  };
  const abortForClose = () => {
    if (!res.writableEnded) abortForClient();
  };
  const cleanupClientListeners = () => {
    req.off("aborted", abortForClient);
    res.off("close", abortForClose);
  };
  req.once("aborted", abortForClient);
  res.once("close", abortForClose);

  if (downstreamDisconnected) {
    cleanupClientListeners();
    auditProxy(principal, name, model, 499, started, requestBytes, 0);
    return;
  }

  let selected;
  try {
    selected = await resolveUpstream(req, name, provider);
  } catch (exc) {
    cleanupClientListeners();
    if (downstreamDisconnected || req.aborted || res.destroyed) {
      auditProxy(principal, name, model, 499, started, requestBytes, 0);
      return;
    }
    console.error(`gateway credential resolution failed for ${name}: ${exc.message}`);
    sendPlain(res, 502, "gateway credential unavailable\n");
    auditProxy(principal, name, model, 502, started, requestBytes, 0);
    return;
  }

  if (downstreamDisconnected || req.aborted || res.destroyed || res.writableEnded) {
    cleanupClientListeners();
    auditProxy(principal, name, model, 499, started, requestBytes, 0);
    return;
  }

  try {
    if (!routeIsCurrent(name, provider, selected.routeIdentity)) {
      throw new Error("route or credential changed while request was pending");
    }
  } catch (exc) {
    cleanupClientListeners();
    console.error(`gateway dispatch snapshot rejected for ${name}: ${exc.message}`);
    if (!res.destroyed) sendPlain(res, 503, "gateway route changed; retry request\n");
    auditProxy(principal, name, model, 503, started, requestBytes, 0);
    return;
  }

  if (downstreamDisconnected || req.aborted || res.destroyed || res.writableEnded) {
    cleanupClientListeners();
    auditProxy(principal, name, model, 499, started, requestBytes, 0);
    return;
  }

  let url;
  try {
    const base = selected.baseUrl.replace(/\/+$/, "");
    url = new URL(base + restPath + search);
  } catch (exc) {
    cleanupClientListeners();
    console.error(`gateway upstream configuration failed for ${name}: ${exc.message}`);
    sendPlain(res, 502, "gateway upstream configuration error\n");
    auditProxy(principal, name, model, 502, started, requestBytes, 0);
    return;
  }

  const timeout = setTimeout(
    () => abort.abort(new Error("upstream request timed out")),
    UPSTREAM_TIMEOUT_MS,
  );
  timeout.unref?.();
  try {
    let upstream;
    if (
      downstreamDisconnected
      || req.aborted
      || res.destroyed
      || res.writableEnded
      || abort.signal.aborted
    ) {
      auditProxy(principal, name, model, 499, started, requestBytes, 0);
      return;
    }
    try {
      upstream = await fetch(url, {
        method: req.method,
        headers: selected.headers,
        body: upstreamBody,
        redirect: "manual",
        signal: abort.signal,
      });
    } catch (exc) {
      const clientCaused = downstreamDisconnected || req.aborted || res.destroyed;
      if (!clientCaused) {
        console.error(`gateway upstream request failed for ${name}: ${exc.message}`);
      }
      if (!res.headersSent && !res.destroyed) {
        sendPlain(res, 502, "upstream request failed\n");
      }
      auditProxy(
        principal,
        name,
        model,
        clientCaused ? 499 : 502,
        started,
        requestBytes,
        0,
      );
      return;
    }

    let responseHeaders;
    let redactor;
    try {
      const relayHeaders = Object.create(null);
      for (const [key, value] of upstream.headers.entries()) {
        if (!DROPPED_RESPONSE_HEADERS.has(key.toLowerCase())) {
          relayHeaders[key] = value;
        }
      }
      responseHeaders = filterResponseHeaders(relayHeaders, selected.sensitiveValues);
      redactor = createResponseSecretRedactor(selected.sensitiveValues);
    } catch (exc) {
      console.error(`gateway response boundary failed closed for ${name}: ${exc.message}`);
      if (!res.headersSent && !res.destroyed) {
        sendPlain(res, 502, "upstream response rejected\n");
      }
      auditProxy(principal, name, model, 502, started, requestBytes, 0);
      return;
    }

    res.writeHead(upstream.status, responseHeaders);
    let responseBytes = 0;
    let upstreamResponseBytes = 0;
    let captureBytes = 0;
    const capture = [];
    try {
      if (upstream.body) {
        for await (const chunk of upstream.body) {
          const bytes = Buffer.from(chunk);
          upstreamResponseBytes += bytes.length;
          if (captureBytes < MAX_USAGE_CAPTURE_BYTES) {
            const remaining = MAX_USAGE_CAPTURE_BYTES - captureBytes;
            const part = bytes.length <= remaining ? bytes : bytes.subarray(0, remaining);
            capture.push(part);
            captureBytes += part.length;
          }
          const safe = redactor.push(bytes);
          await writeWithBackpressure(res, safe);
          responseBytes += safe.length;
        }
      }
      const tail = redactor.flush();
      await writeWithBackpressure(res, tail);
      responseBytes += tail.length;
    } catch (exc) {
      const clientCaused = downstreamDisconnected || req.aborted || res.destroyed;
      if (!clientCaused) {
        console.error(`gateway upstream response failed for ${name}: ${exc.message}`);
      }
      const usage = usageFromCapture(
        Buffer.concat(capture),
        upstream.headers.get("content-type") ?? "",
      );
      usage.capture_truncated = true;
      auditProxy(
        principal,
        name,
        model,
        clientCaused ? 499 : upstream.status,
        started,
        requestBytes,
        responseBytes,
        usage,
      );
      res.destroy();
      return;
    }
    res.end();
    const usage = usageFromCapture(
      Buffer.concat(capture),
      upstream.headers.get("content-type") ?? "",
    );
    usage.capture_truncated = upstreamResponseBytes > captureBytes;
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
  } finally {
    clearTimeout(timeout);
    cleanupClientListeners();
  }
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
        console.error(`gateway request failed for ${match[1]}: ${exc.message}`);
        if (!res.headersSent) {
          sendPlain(res, 502, "gateway request failed\n");
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
    console.log(`cyclo credential gateway listening on :${PORT}`);
    for (const [name, p] of Object.entries(table)) {
      console.log(`provider ${name}: ${p.baseUrl} (${p.api}, ${p.credType})`);
    }
    if (!Object.keys(table).length) console.log("providers: (none discovered)");
  });
  let closing = false;
  const close = () => {
    if (closing) return;
    closing = true;
    server.close(() => process.exit(0));
  };
  process.on("SIGTERM", close);
  process.on("SIGINT", close);
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
