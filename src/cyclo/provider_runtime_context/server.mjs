// Cyclo provider runtime.
//
// This process is deliberately not a credential gateway. It owns composition
// routes and short-lived request contexts, while the unchanged gateway remains
// the only process that reads provider credentials and talks to concrete
// upstreams.

import {
  chmodSync,
  closeSync,
  constants as fsConstants,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, request as httpRequest } from "node:http";
import { isIP } from "node:net";
import { dirname, isAbsolute, join, normalize } from "node:path";
import { pathToFileURL } from "node:url";
import { sanitizeModel } from "./metadata.mjs";
import {
  createResponseSecretRedactor,
  filterResponseHeaders,
  forwardedRequestHeaders,
  isKnownInferenceEndpoint,
  modelFromInferenceRequest,
  prepareRequestBody,
} from "./protocol.mjs";


const STATE_ROOT = process.env.CYCLO_PROVIDER_RUNTIME_STATE
  ?? "/var/lib/cyclo-provider-runtime";
// Production bind-mounts exactly one host-owned file read-only. Tests may
// override it before importing this module; providers can never choose a path.
const HOST_CONFIG_PATH = process.env.CYCLO_HOST_CONFIG ?? "/etc/cyclo/host.conf";
const CLIENTS_JSON_PATH = process.env.CYCLO_PROVIDER_RUNTIME_CLIENTS
  ?? join(STATE_ROOT, "clients.json");
const EXPECTED_PROVIDERS_PATH = process.env.CYCLO_PROVIDER_RUNTIME_EXPECTED
  ?? join(STATE_ROOT, "expected-providers.json");
const REGISTERED_PROVIDERS_PATH = process.env.CYCLO_PROVIDER_RUNTIME_REGISTERED
  ?? join(STATE_ROOT, "registered-providers.json");
const ADMIN_TOKEN_FILE = process.env.CYCLO_PROVIDER_RUNTIME_ADMIN_TOKEN_FILE
  ?? "/run/secrets/cyclo-runtime-admin-token";
const GATEWAY_TOKEN_FILE = process.env.CYCLO_GATEWAY_TOKEN_FILE
  ?? "/run/secrets/cyclo-gateway-token";
const GATEWAY_BASE_URL = process.env.CYCLO_GATEWAY_BASE_URL
  ?? "http://cyclo-gateway:8787";
const PORT = Number(process.env.CYCLO_PROVIDER_RUNTIME_PORT ?? 8788);
const RUNTIME_SOCKET_ROOT = process.env.CYCLO_PROVIDER_RUNTIME_SOCKET_ROOT
  ?? "/run/cyclo/runtime";
const ADMIN_SOCKET = process.env.CYCLO_PROVIDER_RUNTIME_ADMIN_SOCKET
  ?? join(RUNTIME_SOCKET_ROOT, "admin.sock");
const PROVIDER_SOCKET_ROOT = process.env.CYCLO_PROVIDER_SOCKET_ROOT
  ?? "/run/cyclo/providers";

const MAX_HOST_CONFIG_BYTES = 1024 * 1024;
const MAX_REQUEST_BODY = 16 * 1024 * 1024;
const MAX_REGISTRATION_BODY = 64 * 1024;
const MAX_CATALOG_BODY = 16 * 1024 * 1024;
const MAX_TOKEN_BYTES = 64 * 1024;
const MAX_ACTIVE_REQUESTS = 24;
const MAX_ACTIVE_REQUESTS_PER_PRINCIPAL = 8;
const MAX_BUFFERED_REQUESTS = 12;
const MAX_BUFFERED_REQUESTS_PER_PRINCIPAL = 8;
const MAX_NESTED_REQUESTS = 32;
const MAX_NESTED_REQUESTS_PER_ORIGIN = 16;
const MAX_NESTED_BUFFERED_REQUESTS = 24;
const MAX_NESTED_BUFFERED_REQUESTS_PER_ORIGIN = 16;
const MAX_TCP_CONNECTIONS = 256;
const MAX_TCP_CONNECTIONS_PER_INTERFACE = 32;
const MAX_PROVIDER_SOCKET_CONNECTIONS = 64;
const PROVIDER_REQUEST_RATE_PER_SECOND = 200;
const PROVIDER_REQUEST_BURST = 200;
const TCP_INTERFACE_RATE_PER_SECOND = 500;
const TCP_INTERFACE_REQUEST_BURST = 500;
const UPSTREAM_TIMEOUT_MS = 600_000;
const CATALOG_TIMEOUT_MS = 5_000;
const COMPONENT_HEALTH_TIMEOUT_MS = 3_000;
const INBOUND_BODY_TIMEOUT_MS = environmentInteger(
  "CYCLO_PROVIDER_RUNTIME_INBOUND_TIMEOUT_MS",
  30_000,
  { minimum: 100, maximum: 300_000 },
);
const AUTHORITY_WATCH_INTERVAL_MS = environmentInteger(
  "CYCLO_PROVIDER_RUNTIME_AUTHORITY_WATCH_MS",
  500,
  { minimum: 100, maximum: 60_000 },
);
const REGISTRATION_ATTEMPT_INTERVAL_MS = 100;
const EXACT_REGISTRATION_INTERVAL_MS = 1_000;
const REGISTRATION_REWRITE_INTERVAL_MS = 5_000;
const GLOBAL_REGISTRATION_REWRITE_INTERVAL_MS = 1_000;
const MAX_COMPONENT_HEALTH_BODY = 1024;
const REQUEST_CONTEXT_HEADER = "x-cyclo-request-context";
const RUNTIME_BOOT_ID_HEADER = "x-cyclo-runtime-boot-id";
const RUNTIME_BOOT_ID = randomBytes(16).toString("hex");
const REQUEST_CONTEXT_TTL_MS = UPSTREAM_TIMEOUT_MS + 30_000;
const MAX_NESTED_PROVIDER_CALLS = 16;
const MAX_PROVIDER_CHAIN_DEPTH = 16;
const ROUTE_NAME = /^[a-z0-9_-]+$/u;
const API_NAME = /^[a-z0-9_-]+$/u;
const PARAMETER_NAME = /^[a-z][a-z0-9_-]*$/u;
const PROVIDER_SOCKET_ID = /^[a-f0-9]{32}$/u;
const RESERVED_ROUTE_NAMES = new Set(["__proto__", "constructor", "gateway", "prototype"]);
const CLIENT_KINDS = new Set(["client", "team", "provider"]);
const TRANSPORT_TCP = "tcp";
const TRANSPORT_PROVIDER_UDS = "provider-uds";
const TRANSPORT_ADMIN_UDS = "admin-uds";
const REQUEST_TRANSPORT = Symbol("requestTransport");
const REQUEST_PROVIDER_PREFIX = Symbol("requestProviderPrefix");
const CONTROL_RELOAD_PATH = "/_cyclo/v1/control/reload";
const CONTROL_REFRESH_CATALOG_PATH = "/_cyclo/v1/control/refresh-catalog";
const CONTROL_PATHS = new Set([CONTROL_RELOAD_PATH, CONTROL_REFRESH_CATALOG_PATH]);

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
  "content-length",
  REQUEST_CONTEXT_HEADER,
  "x-api-key",
]);


function environmentInteger(name, fallback, { minimum, maximum }) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
  }
  return value;
}


function gatewayOrigin() {
  let parsed;
  try {
    parsed = new URL(GATEWAY_BASE_URL);
  } catch (error) {
    throw new Error(`invalid CYCLO_GATEWAY_BASE_URL: ${error.message}`);
  }
  if (
    !new Set(["http:", "https:"]).has(parsed.protocol)
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || !["", "/"].includes(parsed.pathname)
  ) {
    throw new Error("CYCLO_GATEWAY_BASE_URL must be an HTTP(S) origin without credentials");
  }
  return parsed.origin;
}

const GATEWAY_ORIGIN = gatewayOrigin();


function validRouteName(value) {
  return typeof value === "string"
    && ROUTE_NAME.test(value)
    && !RESERVED_ROUTE_NAMES.has(value);
}


function safeGeneration(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 256
    && !/[\u0000-\u001f\u007f]/u.test(value);
}


function safeModelId(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 1024
    && !value.split("").some((character) => {
      const code = character.charCodeAt(0);
      return character.trim() === "" || code < 0x20 || code === 0x7f;
    });
}


function inputModel(value) {
  if (typeof value !== "string") return false;
  const separator = value.indexOf("/");
  return separator > 0
    && validRouteName(value.slice(0, separator))
    && safeModelId(value.slice(separator + 1));
}


function objectDocument(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value;
}


function readJson(path, { missing = null } = {}) {
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") return missing;
    throw new Error(`cannot read ${path}: ${error.message}`);
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`cannot parse ${path}: ${error.message}`);
  }
}


function readTokenFile(path, { optional = false } = {}) {
  let descriptor = -1;
  try {
    descriptor = openSync(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    const metadata = fstatSync(descriptor);
    if (!metadata.isFile() || metadata.size <= 0 || metadata.size > MAX_TOKEN_BYTES) {
      throw new Error("token path is not a bounded regular file");
    }
    const bytes = Buffer.alloc(metadata.size);
    const count = readSync(descriptor, bytes, 0, bytes.length, 0);
    const value = bytes.subarray(0, count).toString("utf8").trim();
    if (!value || [...value].some((character) => /\s/u.test(character))) {
      throw new Error("token is empty or contains whitespace");
    }
    return value;
  } catch (error) {
    if (optional && error?.code === "ENOENT") return null;
    throw new Error(`cannot read capability ${path}: ${error.message}`);
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
  }
}


function loadHostConfig() {
  let bytes;
  try {
    bytes = readFileSync(HOST_CONFIG_PATH);
  } catch (error) {
    if (error?.code === "ENOENT") {
      return Object.freeze({ definitions: [], byPrefix: Object.create(null), revision: "missing" });
    }
    throw new Error(`cannot read host configuration ${HOST_CONFIG_PATH}: ${error.message}`);
  }
  if (bytes.length > MAX_HOST_CONFIG_BYTES) {
    throw new Error(`host configuration exceeds ${MAX_HOST_CONFIG_BYTES} bytes`);
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error(`host configuration is not valid UTF-8: ${HOST_CONFIG_PATH}`);
  }
  const definitions = [];
  const byPrefix = Object.create(null);
  for (const [offset, raw] of text.split(/\r?\n/u).entries()) {
    const lineNumber = offset + 1;
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const fields = line.split(/\s+/u);
    if (fields[0] !== "provider" || fields.length < 4) {
      throw new Error(
        `${HOST_CONFIG_PATH}:${lineNumber}: expected provider PREFIX PATH INPUT... [key=value ...]`,
      );
    }
    const prefix = fields[1];
    if (!validRouteName(prefix)) {
      throw new Error(`${HOST_CONFIG_PATH}:${lineNumber}: invalid provider prefix ${prefix}`);
    }
    if (Object.hasOwn(byPrefix, prefix)) {
      throw new Error(`${HOST_CONFIG_PATH}:${lineNumber}: duplicate provider prefix ${prefix}`);
    }
    const inputs = [];
    const parameters = [];
    const parameterNames = new Set();
    let sawParameter = false;
    for (const value of fields.slice(3)) {
      if (!value.includes("=")) {
        if (sawParameter || !inputModel(value)) {
          throw new Error(`${HOST_CONFIG_PATH}:${lineNumber}: invalid provider input ${value}`);
        }
        inputs.push(value);
        continue;
      }
      sawParameter = true;
      const split = value.indexOf("=");
      const key = value.slice(0, split);
      if (!PARAMETER_NAME.test(key) || parameterNames.has(key)) {
        throw new Error(`${HOST_CONFIG_PATH}:${lineNumber}: invalid provider parameter ${key}`);
      }
      parameterNames.add(key);
      parameters.push([key, value.slice(split + 1)]);
    }
    if (!inputs.length || new Set(inputs).size !== inputs.length) {
      throw new Error(`${HOST_CONFIG_PATH}:${lineNumber}: provider inputs must be nonempty and unique`);
    }
    // PATH is intentionally opaque here. Only the host-side lifecycle resolves,
    // validates, builds, or mounts provider source directories.
    const definition = Object.freeze({
      prefix,
      path: fields[2],
      arguments: Object.freeze(fields.slice(3)),
      inputs: Object.freeze(inputs),
      parameters: Object.freeze(parameters),
      line: lineNumber,
      configuration_sha256: createHash("sha256")
        .update(JSON.stringify([prefix, fields[2], ...fields.slice(3)]), "utf8")
        .digest("hex"),
    });
    definitions.push(definition);
    byPrefix[prefix] = definition;
  }
  const configured = new Set(definitions.map((definition) => definition.prefix));
  const earlier = new Set();
  for (const definition of definitions) {
    for (const reference of definition.inputs) {
      const dependency = reference.slice(0, reference.indexOf("/"));
      if (configured.has(dependency) && !earlier.has(dependency)) {
        throw new Error(
          `${HOST_CONFIG_PATH}:${definition.line}: provider input ${reference} is a forward or self reference`,
        );
      }
    }
    earlier.add(definition.prefix);
  }
  return Object.freeze({
    definitions: Object.freeze(definitions),
    byPrefix: Object.freeze(byPrefix),
    revision: createHash("sha256").update(bytes).digest("hex"),
  });
}


function componentSocketPath(prefix, value) {
  const root = normalize(PROVIDER_SOCKET_ROOT);
  const relative = typeof value === "string" && value.startsWith(`${root}/`)
    ? value.slice(root.length + 1).split("/")
    : [];
  if (
    typeof value !== "string"
    || !isAbsolute(root)
    || normalize(PROVIDER_SOCKET_ROOT) !== PROVIDER_SOCKET_ROOT
    || !isAbsolute(value)
    || normalize(value) !== value
    || relative.length !== 2
    || !PROVIDER_SOCKET_ID.test(relative[0])
    || relative[1] !== "provider.sock"
    || Buffer.byteLength(value) > 103
  ) {
    throw new Error(
      `expected provider socket for ${prefix} must match ${root}/<32 hex>/provider.sock`,
    );
  }
  return value;
}


function providerRuntimeSocketPath(prefix) {
  const root = normalize(RUNTIME_SOCKET_ROOT);
  const socketId = createHash("sha256").update(prefix).digest("hex").slice(0, 32);
  const value = join(root, socketId, "runtime.sock");
  if (
    !isAbsolute(root)
    || normalize(RUNTIME_SOCKET_ROOT) !== RUNTIME_SOCKET_ROOT
    || Buffer.byteLength(value) > 103
  ) {
    throw new Error("provider runtime socket root must be a normalized absolute path");
  }
  return value;
}


function expectedProviderRegistry() {
  const raw = readJson(EXPECTED_PROVIDERS_PATH, { missing: { version: 1, providers: [] } });
  const document = objectDocument(raw, `expected provider state ${EXPECTED_PROVIDERS_PATH}`);
  if (document.version !== 1 || !Array.isArray(document.providers)) {
    throw new Error(`expected provider state ${EXPECTED_PROVIDERS_PATH} must have version 1`);
  }
  const table = Object.create(null);
  for (const entry of document.providers) {
    const item = objectDocument(entry, "expected provider entry");
    const allowed = new Set([
      "prefix",
      "generation",
      "configuration_sha256",
      "token_sha256",
      "inputs",
      "socket_path",
    ]);
    if (Object.keys(item).some((key) => !allowed.has(key))) {
      throw new Error("expected provider entry contains an unknown field");
    }
    if (!validRouteName(item.prefix) || Object.hasOwn(table, item.prefix)) {
      throw new Error("expected provider state contains an invalid or duplicate prefix");
    }
    if (!safeGeneration(item.generation)) {
      throw new Error(`expected provider ${item.prefix} has an invalid generation`);
    }
    if (
      item.configuration_sha256 !== undefined
      && (
        typeof item.configuration_sha256 !== "string"
        || !/^[a-f0-9]{64}$/u.test(item.configuration_sha256)
      )
    ) {
      throw new Error(`expected provider ${item.prefix} has an invalid configuration hash`);
    }
    if (typeof item.token_sha256 !== "string" || !/^[a-f0-9]{64}$/u.test(item.token_sha256)) {
      throw new Error(`expected provider ${item.prefix} has an invalid token hash`);
    }
    if (
      !Array.isArray(item.inputs)
      || !item.inputs.length
      || new Set(item.inputs).size !== item.inputs.length
      || !item.inputs.every(inputModel)
    ) {
      throw new Error(`expected provider ${item.prefix} has invalid inputs`);
    }
    table[item.prefix] = Object.freeze({
      prefix: item.prefix,
      generation: item.generation,
      configuration_sha256: item.configuration_sha256 ?? null,
      token_sha256: item.token_sha256,
      inputs: Object.freeze([...item.inputs]),
      socket_path: componentSocketPath(item.prefix, item.socket_path),
    });
  }
  return Object.freeze(table);
}


function registeredProviderDocument() {
  const raw = readJson(
    REGISTERED_PROVIDERS_PATH,
    { missing: { version: 1, providers: Object.create(null) } },
  );
  const document = objectDocument(raw, `registered provider state ${REGISTERED_PROVIDERS_PATH}`);
  if (
    document.version !== 1
    || !document.providers
    || typeof document.providers !== "object"
    || Array.isArray(document.providers)
  ) {
    throw new Error(`registered provider state ${REGISTERED_PROVIDERS_PATH} must have version 1`);
  }
  return document;
}


function registryClients() {
  const raw = readJson(CLIENTS_JSON_PATH, { missing: { version: 1, clients: [] } });
  const document = objectDocument(raw, `runtime clients ${CLIENTS_JSON_PATH}`);
  if (document.version !== 1 || !Array.isArray(document.clients)) {
    throw new Error(`runtime clients ${CLIENTS_JSON_PATH} must have version 1`);
  }
  const clients = [];
  const tokenHashes = new Set();
  const clientIds = new Set();
  const providerPrefixes = new Set();
  for (const rawEntry of document.clients) {
    const entry = objectDocument(rawEntry, "runtime client entry");
    if (
      typeof entry.client_id !== "string"
      || !entry.client_id
      || clientIds.has(entry.client_id)
      || typeof entry.token_sha256 !== "string"
      || !/^[a-f0-9]{64}$/u.test(entry.token_sha256)
      || tokenHashes.has(entry.token_sha256)
      || !CLIENT_KINDS.has(entry.kind)
      || !Array.isArray(entry.providers)
      || !entry.providers.every((value) => typeof value === "string")
      || !Array.isArray(entry.models)
      || !entry.models.every((value) => typeof value === "string")
    ) {
      throw new Error("runtime clients contain an invalid or duplicate capability");
    }
    const localAddresses = entry.kind === "provider"
      ? []
      : (entry.local_addresses ?? []);
    if (
      !Array.isArray(localAddresses)
      || localAddresses.length > 16
      || new Set(localAddresses).size !== localAddresses.length
      || !localAddresses.every((value) => typeof value === "string" && isIP(value) !== 0)
    ) {
      throw new Error("runtime client contains invalid local network bindings");
    }
    if (
      entry.kind === "provider"
      && (
        !validRouteName(entry.provider_prefix)
        || providerPrefixes.has(entry.provider_prefix)
        || !safeGeneration(entry.binding_generation)
      )
    ) {
      throw new Error("runtime clients contain an invalid or duplicate provider binding");
    }
    clientIds.add(entry.client_id);
    tokenHashes.add(entry.token_sha256);
    if (entry.kind === "provider") providerPrefixes.add(entry.provider_prefix);
    clients.push(Object.freeze({
      ...entry,
      local_addresses: Object.freeze([...localAddresses]),
    }));
  }
  return Object.freeze(clients);
}


function authorityFileIdentity(path) {
  let metadata;
  try {
    metadata = lstatSync(path, { bigint: true, throwIfNoEntry: false });
  } catch (error) {
    throw new Error(`cannot inspect runtime authority ${path}: ${error.message}`);
  }
  if (!metadata) return `${path}:missing`;
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error(`runtime authority is not a real file: ${path}`);
  }
  return [
    path,
    metadata.dev,
    metadata.ino,
    metadata.size,
    metadata.mtimeNs,
    metadata.ctimeNs,
  ].join(":");
}


function authorityFilesIdentity() {
  return [CLIENTS_JSON_PATH, EXPECTED_PROVIDERS_PATH]
    .map(authorityFileIdentity)
    .join("|");
}


function loadAuthorityFiles() {
  // Both writers use atomic replacement. Comparing identities around the
  // reads prevents one snapshot from combining files from two transitions.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const before = authorityFilesIdentity();
    const expected = expectedProviderRegistry();
    const clients = registryClients();
    const after = authorityFilesIdentity();
    if (before === after) return Object.freeze({ expected, clients, identity: after });
  }
  throw new Error("runtime authority changed repeatedly while being loaded");
}


function indexClients(clients) {
  const index = new Map();
  for (const entry of clients) index.set(entry.token_sha256, entry);
  return index;
}


function indexProviderClients(clients) {
  const index = Object.create(null);
  for (const entry of clients) {
    if (entry.kind === "provider" && validRouteName(entry.provider_prefix)) {
      index[entry.provider_prefix] = entry;
    }
  }
  return Object.freeze(index);
}


function expectedForDefinition(configuration, expected, prefix) {
  const definition = configuration.byPrefix[prefix];
  const item = expected[prefix];
  if (!definition || !item) return null;
  if (
    item.configuration_sha256 !== definition.configuration_sha256
    || item.inputs.length !== definition.inputs.length
    || item.inputs.some((value, index) => value !== definition.inputs[index])
  ) {
    return null;
  }
  return item;
}


function digestToken(token) {
  return createHash("sha256").update(token).digest();
}


function hashMatches(digest, hex) {
  if (typeof hex !== "string" || !/^[a-f0-9]{64}$/iu.test(hex)) return false;
  const expected = Buffer.from(hex, "hex");
  return expected.length === digest.length && timingSafeEqual(digest, expected);
}


function presentedToken(req) {
  const authorization = req.headers.authorization;
  const value = typeof authorization === "string" && authorization.startsWith("Bearer ")
    ? authorization.slice(7)
    : req.headers["x-api-key"];
  return typeof value === "string" && value ? value : null;
}


function expiryMillis(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value < 10_000_000_000 ? value * 1000 : value;
  }
  if (typeof value === "string") return Date.parse(value);
  return Number.NaN;
}


function principalFromEntry(entry, token) {
  const provider = entry?.kind === "provider";
  return Object.freeze({
    kind: provider ? "provider" : "client",
    client_id: entry.client_id,
    team_id: typeof entry.team_id === "string" ? entry.team_id : entry.client_id,
    provider_prefix: provider && typeof entry.provider_prefix === "string"
      ? entry.provider_prefix
      : null,
    binding_generation: typeof entry.binding_generation === "string" && entry.binding_generation
      ? entry.binding_generation
      : null,
    providers: Object.freeze(
      Array.isArray(entry.providers)
        ? entry.providers.filter((value) => typeof value === "string")
        : [],
    ),
    models: Object.freeze(
      Array.isArray(entry.models)
        ? entry.models.filter((value) => typeof value === "string")
        : [],
    ),
    local_addresses: Object.freeze(
      Array.isArray(entry.local_addresses) ? [...entry.local_addresses] : [],
    ),
    token,
  });
}


function authenticate(req, state, snapshot, now = Date.now()) {
  const transport = req[REQUEST_TRANSPORT] ?? TRANSPORT_TCP;
  const token = presentedToken(req);
  if (!token) return null;
  const digest = digestToken(token);
  if (transport === TRANSPORT_ADMIN_UDS) {
    const admin = state.adminToken;
    if (admin && timingSafeEqual(digest, digestToken(admin))) {
      return Object.freeze({
        kind: "admin",
        client_id: "admin",
        team_id: "admin",
        provider_prefix: null,
        binding_generation: null,
        providers: Object.freeze(["*"]),
        models: Object.freeze(["*"]),
        token,
      });
    }
  }
  const entry = snapshot.clientIndex.get(digest.toString("hex"));
  if (entry) {
    const expiresAt = expiryMillis(entry.expires_at);
    if (entry.enabled === false || entry.revoked === true) return null;
    if (expiresAt !== null && (!Number.isFinite(expiresAt) || now >= expiresAt)) return null;
    const principal = principalFromEntry(entry, token);
    if (principal.kind === "provider") {
      if (
        transport !== TRANSPORT_PROVIDER_UDS
        || !validRouteName(principal.provider_prefix)
        || req[REQUEST_PROVIDER_PREFIX] !== principal.provider_prefix
      ) {
        return null;
      }
      const item = expectedForDefinition(
        snapshot.configuration,
        snapshot.expected,
        principal.provider_prefix,
      );
      if (!item || principal.binding_generation !== item.generation) return null;
    } else if (transport !== TRANSPORT_TCP) {
      return null;
    } else {
      const rawAddress = req.socket?.localAddress;
      const localAddress = typeof rawAddress === "string"
        && rawAddress.startsWith("::ffff:")
        && isIP(rawAddress.slice(7)) === 4
        ? rawAddress.slice(7)
        : rawAddress;
      if (!principal.local_addresses.includes(localAddress)) return null;
    }
    return principal;
  }
  return null;
}


function principalAllowsProvider(principal, provider) {
  return principal?.kind === "admin"
    || principal?.providers?.includes("*")
    || principal?.providers?.includes(provider);
}


function principalAllowsModel(principal, provider, model) {
  return principal?.kind === "admin"
    || principal?.models?.includes("*")
    || principal?.models?.includes(`${provider}/${model}`);
}


function sanitizedComponentModels(models) {
  if (!Array.isArray(models) || !models.length || models.length > 256) {
    throw new Error("provider catalog models must be a nonempty bounded array");
  }
  const clean = [];
  const ids = new Set();
  for (const model of models) {
    const sanitized = sanitizeModel(model);
    if (!sanitized || !safeModelId(sanitized.id)) {
      throw new Error("provider catalog contains an invalid model");
    }
    if (ids.has(sanitized.id)) {
      const error = new Error(`provider catalog repeats model ${sanitized.id}`);
      error.statusCode = 409;
      throw error;
    }
    ids.add(sanitized.id);
    clean.push(sanitized);
  }
  return clean;
}


function sanitizeConcreteCatalog(value) {
  const document = objectDocument(value, "gateway provider catalog");
  const catalog = Object.create(null);
  for (const [prefix, raw] of Object.entries(document)) {
    if (!validRouteName(prefix)) throw new Error("gateway returned an invalid provider prefix");
    const info = objectDocument(raw, `gateway provider ${prefix}`);
    if (typeof info.api !== "string" || !info.api || !Array.isArray(info.models)) {
      throw new Error(`gateway provider ${prefix} returned an invalid catalog`);
    }
    const models = [];
    const ids = new Set();
    for (const model of info.models) {
      const clean = sanitizeModel(model);
      if (!clean || !safeModelId(clean.id) || ids.has(clean.id)) {
        throw new Error(`gateway provider ${prefix} returned invalid model metadata`);
      }
      ids.add(clean.id);
      models.push(clean);
    }
    catalog[prefix] = Object.freeze({ api: info.api, models: Object.freeze(models) });
  }
  return Object.freeze(catalog);
}


async function boundedWebBody(response, limit = MAX_CATALOG_BODY) {
  const chunks = [];
  let size = 0;
  if (response.body) {
    for await (const chunk of response.body) {
      const bytes = Buffer.from(chunk);
      size += bytes.length;
      if (size > limit) throw new Error("gateway response is too large");
      chunks.push(bytes);
    }
  }
  return Buffer.concat(chunks, size);
}


async function gatewayCatalog(token, { signal = null } = {}) {
  const response = await fetch(`${GATEWAY_ORIGIN}/providers`, {
    headers: { authorization: `Bearer ${token}` },
    redirect: "manual",
    signal: signal ?? AbortSignal.timeout(CATALOG_TIMEOUT_MS),
  });
  const body = await boundedWebBody(response);
  if (response.status !== 200) {
    throw new Error(`gateway catalog returned ${response.status}`);
  }
  let document;
  try {
    document = JSON.parse(body.toString("utf8"));
  } catch {
    throw new Error("gateway catalog is not valid JSON");
  }
  return sanitizeConcreteCatalog(document);
}


function registeredRoute(definition, expected, raw) {
  if (
    !raw
    || typeof raw !== "object"
    || Array.isArray(raw)
    || raw.generation !== expected.generation
    || typeof raw.api !== "string"
    || !API_NAME.test(raw.api)
    || typeof raw.ingress_token !== "string"
    || !raw.ingress_token
    || typeof raw.socket_identity !== "string"
    || !/^[0-9]+:[0-9]+$/u.test(raw.socket_identity)
    || (
      raw.registration_id !== undefined
      && (
        typeof raw.registration_id !== "string"
        || !/^[a-f0-9]{32}$/u.test(raw.registration_id)
      )
    )
    || !hashMatches(digestToken(raw.ingress_token), expected.token_sha256)
  ) {
    return null;
  }
  let models;
  try {
    models = sanitizedComponentModels(raw.models);
  } catch {
    return null;
  }
  return Object.freeze({
    kind: "component",
    prefix: definition.prefix,
    generation: expected.generation,
    api: raw.api,
    models: Object.freeze(models),
    ingress_token: raw.ingress_token,
    registration_id: raw.registration_id ?? null,
    registered_at: typeof raw.registered_at === "string"
      && Number.isFinite(Date.parse(raw.registered_at))
      ? raw.registered_at
      : null,
    socket_path: expected.socket_path,
    socket_identity: raw.socket_identity,
    inputs: definition.inputs,
  });
}


function catalogHasModel(catalog, reference) {
  const separator = reference.indexOf("/");
  const prefix = reference.slice(0, separator);
  const model = reference.slice(separator + 1);
  return catalog[prefix]?.models?.some((item) => item.id === model) === true;
}


function cloneRegisteredProviders(providers) {
  const clone = Object.create(null);
  for (const [prefix, raw] of Object.entries(providers)) {
    clone[prefix] = raw && typeof raw === "object" && !Array.isArray(raw)
      ? { ...raw, ...(Array.isArray(raw.models) ? { models: [...raw.models] } : {}) }
      : raw;
  }
  return clone;
}


function buildRouteSnapshot({
  configuration,
  expected,
  clients,
  registered,
  concrete,
  epoch,
  revision,
}) {
  const effectiveConcrete = Object.assign(Object.create(null), concrete);
  const components = Object.create(null);
  const collisions = new Set(
    configuration.definitions
      .filter((definition) => Object.hasOwn(concrete, definition.prefix))
      .map((definition) => definition.prefix),
  );
  for (const prefix of collisions) delete effectiveConcrete[prefix];
  const catalog = Object.assign(Object.create(null), effectiveConcrete);
  const providerClients = indexProviderClients(clients);
  for (const definition of configuration.definitions) {
    if (collisions.has(definition.prefix)) continue;
    const authorized = expectedForDefinition(configuration, expected, definition.prefix);
    if (!authorized) continue;
    const providerClient = providerClients[definition.prefix];
    if (
      providerClient?.binding_generation !== authorized.generation
      || providerClient.enabled === false
      || providerClient.revoked === true
    ) {
      continue;
    }
    const route = registeredRoute(definition, authorized, registered[definition.prefix]);
    if (!route) continue;
    if (!definition.inputs.every((reference) => catalogHasModel(catalog, reference))) continue;
    components[definition.prefix] = route;
    catalog[definition.prefix] = Object.freeze({
      kind: "component",
      generation: route.generation,
      registered_at: route.registered_at,
      api: route.api,
      models: route.models,
    });
  }
  return Object.freeze({
    epoch,
    revision,
    configuration,
    expected,
    clients,
    clientIndex: indexClients(clients),
    providerClients,
    concrete: Object.freeze(effectiveConcrete),
    components: Object.freeze(components),
    catalog: Object.freeze(catalog),
    collisions,
  });
}


class RuntimeState {
  constructor({
    configuration,
    expected,
    clients,
    authorityIdentity,
    registered,
    concrete,
    adminToken,
    gatewayToken,
  }) {
    this.configuration = configuration;
    this.expected = expected;
    this.clients = clients;
    this.registered = registered;
    this.concrete = concrete;
    this.adminToken = adminToken;
    this.gatewayToken = gatewayToken;
    this.requestContexts = new Map();
    this.activeRequests = 0;
    this.activeRequestsByPrincipal = new Map();
    this.bufferedRequests = 0;
    this.bufferedRequestsByPrincipal = new Map();
    this.nestedRequests = 0;
    this.nestedRequestsByOrigin = new Map();
    this.nestedBufferedRequests = 0;
    this.nestedBufferedRequestsByOrigin = new Map();
    this.writeTail = Promise.resolve();
    this.authorityObservedIdentity = authorityIdentity;
    this.authorityReloadFailed = false;
    this.authorityRetryAt = 0;
    this.authorityWatchTimer = null;
    this.authorityWatchBusy = false;
    this.epoch = 0;
    this.revision = 0;
    this.catalogRequest = 0;
    this.catalogApplied = 0;
    this.registrationLeases = Object.create(null);
    this.providerRequestBuckets = new Map();
    this.tcpRequestBuckets = new Map();
    this.registrationRequests = new Set();
    this.registrationRequestAt = Object.create(null);
    this.changedRegistrations = new Set();
    this.changedRegistrationAt = Object.create(null);
    this.changedRegistrationGlobalAt = Number.NEGATIVE_INFINITY;
    this.exactRegistrationAt = Object.create(null);
    const recoveredAt = Date.now();
    for (const prefix of Object.keys(registered)) {
      this.registrationLeases[prefix] = randomBytes(16).toString("hex");
      this.changedRegistrationAt[prefix] = recoveredAt;
      this.changedRegistrationGlobalAt = recoveredAt;
    }
    this.current = null;
    this.publish({ invalidateContexts: true });
  }

  snapshot() {
    return this.current;
  }

  publish({ invalidateContexts = false } = {}) {
    this.revision += 1;
    if (invalidateContexts) {
      this.epoch += 1;
      this.requestContexts.clear();
    }
    this.current = buildRouteSnapshot({
      configuration: this.configuration,
      expected: this.expected,
      clients: this.clients,
      registered: this.registered,
      concrete: this.concrete,
      epoch: this.epoch,
      revision: this.revision,
    });
    return this.current;
  }

  serialize(operation) {
    const result = this.writeTail.then(operation, operation);
    this.writeTail = result.catch(() => {});
    return result;
  }

  persistRegistered(registered) {
    writeJsonAtomic(REGISTERED_PROVIDERS_PATH, {
      version: 1,
      providers: registered,
    });
  }

  admitProviderTransport(prefix, now = Date.now()) {
    const previous = this.providerRequestBuckets.get(prefix) ?? {
      tokens: PROVIDER_REQUEST_BURST,
      updatedAt: now,
    };
    const elapsed = Math.max(0, now - previous.updatedAt);
    const tokens = Math.min(
      PROVIDER_REQUEST_BURST,
      previous.tokens + elapsed * PROVIDER_REQUEST_RATE_PER_SECOND / 1000,
    );
    if (tokens < 1) {
      this.providerRequestBuckets.set(prefix, { tokens, updatedAt: now });
      return false;
    }
    this.providerRequestBuckets.set(prefix, { tokens: tokens - 1, updatedAt: now });
    return true;
  }

  admitTcpTransport(address, now = Date.now()) {
    const identity = normalizedIpAddress(address) ?? "unknown";
    const previous = this.tcpRequestBuckets.get(identity) ?? {
      tokens: TCP_INTERFACE_REQUEST_BURST,
      updatedAt: now,
    };
    const elapsed = Math.max(0, now - previous.updatedAt);
    const tokens = Math.min(
      TCP_INTERFACE_REQUEST_BURST,
      previous.tokens + elapsed * TCP_INTERFACE_RATE_PER_SECOND / 1000,
    );
    if (tokens < 1) {
      this.tcpRequestBuckets.set(identity, { tokens, updatedAt: now });
      return false;
    }
    this.tcpRequestBuckets.set(identity, { tokens: tokens - 1, updatedAt: now });
    return true;
  }

  acquireRequest(principal) {
    if (principal.kind === "admin") {
      // The host-only control capability has a reserved path even when hostile
      // teams have filled the ordinary shared workload budget.
      return Object.freeze({ status: 200, release() {} });
    }
    const identity = principal.kind === "provider"
      ? `provider:${principal.provider_prefix}:${principal.binding_generation}`
      : `client:${principal.client_id}:${principal.binding_generation}`;
    if (this.activeRequests >= MAX_ACTIVE_REQUESTS) {
      return Object.freeze({ status: 503, release: null });
    }
    const current = this.activeRequestsByPrincipal.get(identity) ?? 0;
    if (current >= MAX_ACTIVE_REQUESTS_PER_PRINCIPAL) {
      return Object.freeze({ status: 429, release: null });
    }
    this.activeRequests += 1;
    this.activeRequestsByPrincipal.set(identity, current + 1);
    let released = false;
    return Object.freeze({
      status: 200,
      release: () => {
        if (released) return;
        released = true;
        this.activeRequests -= 1;
        const remaining = (this.activeRequestsByPrincipal.get(identity) ?? 1) - 1;
        if (remaining > 0) this.activeRequestsByPrincipal.set(identity, remaining);
        else this.activeRequestsByPrincipal.delete(identity);
      },
    });
  }

  acquireBufferedRequest(principal) {
    if (principal.kind === "admin") {
      return Object.freeze({ status: 200, release() {} });
    }
    const identity = principal.kind === "provider"
      ? `provider:${principal.provider_prefix}:${principal.binding_generation}`
      : `client:${principal.client_id}:${principal.binding_generation}`;
    if (this.bufferedRequests >= MAX_BUFFERED_REQUESTS) {
      return Object.freeze({ status: 503, release: null });
    }
    const current = this.bufferedRequestsByPrincipal.get(identity) ?? 0;
    if (current >= MAX_BUFFERED_REQUESTS_PER_PRINCIPAL) {
      return Object.freeze({ status: 429, release: null });
    }
    this.bufferedRequests += 1;
    this.bufferedRequestsByPrincipal.set(identity, current + 1);
    let released = false;
    return Object.freeze({
      status: 200,
      release: () => {
        if (released) return;
        released = true;
        this.bufferedRequests -= 1;
        const remaining = (this.bufferedRequestsByPrincipal.get(identity) ?? 1) - 1;
        if (remaining > 0) this.bufferedRequestsByPrincipal.set(identity, remaining);
        else this.bufferedRequestsByPrincipal.delete(identity);
      },
    });
  }

  acquireNestedRequest(origin) {
    if (origin.kind === "admin") {
      return Object.freeze({ status: 200, release() {} });
    }
    const identity = `origin:${origin.client_id}:${origin.binding_generation}`;
    if (this.nestedRequests >= MAX_NESTED_REQUESTS) {
      return Object.freeze({ status: 503, release: null });
    }
    const current = this.nestedRequestsByOrigin.get(identity) ?? 0;
    if (current >= MAX_NESTED_REQUESTS_PER_ORIGIN) {
      return Object.freeze({ status: 429, release: null });
    }
    this.nestedRequests += 1;
    this.nestedRequestsByOrigin.set(identity, current + 1);
    let released = false;
    return Object.freeze({
      status: 200,
      release: () => {
        if (released) return;
        released = true;
        this.nestedRequests -= 1;
        const remaining = (this.nestedRequestsByOrigin.get(identity) ?? 1) - 1;
        if (remaining > 0) this.nestedRequestsByOrigin.set(identity, remaining);
        else this.nestedRequestsByOrigin.delete(identity);
      },
    });
  }

  acquireNestedBufferedRequest(origin) {
    if (origin.kind === "admin") {
      return Object.freeze({ status: 200, release() {} });
    }
    const identity = `origin:${origin.client_id}:${origin.binding_generation}`;
    if (this.nestedBufferedRequests >= MAX_NESTED_BUFFERED_REQUESTS) {
      return Object.freeze({ status: 503, release: null });
    }
    const current = this.nestedBufferedRequestsByOrigin.get(identity) ?? 0;
    if (current >= MAX_NESTED_BUFFERED_REQUESTS_PER_ORIGIN) {
      return Object.freeze({ status: 429, release: null });
    }
    this.nestedBufferedRequests += 1;
    this.nestedBufferedRequestsByOrigin.set(identity, current + 1);
    let released = false;
    return Object.freeze({
      status: 200,
      release: () => {
        if (released) return;
        released = true;
        this.nestedBufferedRequests -= 1;
        const remaining = (this.nestedBufferedRequestsByOrigin.get(identity) ?? 1) - 1;
        if (remaining > 0) this.nestedBufferedRequestsByOrigin.set(identity, remaining);
        else this.nestedBufferedRequestsByOrigin.delete(identity);
      },
    });
  }

  async reconcileAuthorityFiles(now = Date.now()) {
    let observed;
    try {
      observed = authorityFilesIdentity();
    } catch {
      observed = null;
    }
    if (
      observed === this.authorityObservedIdentity
      && (!this.authorityReloadFailed || now < this.authorityRetryAt)
    ) {
      return this.snapshot();
    }
    this.authorityObservedIdentity = observed;
    return this.reloadControl();
  }

  startAuthorityWatch(intervalMs = AUTHORITY_WATCH_INTERVAL_MS) {
    if (this.authorityWatchTimer !== null) return;
    this.authorityWatchTimer = setInterval(() => {
      if (this.authorityWatchBusy) return;
      this.authorityWatchBusy = true;
      this.reconcileAuthorityFiles().catch((error) => {
        console.error(`provider runtime authority watch failed closed: ${error.message}`);
      }).finally(() => {
        this.authorityWatchBusy = false;
      });
    }, intervalMs);
    this.authorityWatchTimer.unref?.();
  }

  stopAuthorityWatch() {
    if (this.authorityWatchTimer === null) return;
    clearInterval(this.authorityWatchTimer);
    this.authorityWatchTimer = null;
  }

  async reloadControl() {
    return this.serialize(async () => {
      let authority;
      try {
        authority = loadAuthorityFiles();
      } catch (error) {
        // A changed but malformed authority file can never leave old bearers
        // live. Keep host.conf and the concrete catalogue, revoke all dynamic
        // clients/routes, and recover on the next valid replacement.
        try {
          this.authorityObservedIdentity = authorityFilesIdentity();
        } catch {
          this.authorityObservedIdentity = null;
        }
        this.authorityReloadFailed = true;
        this.authorityRetryAt = Date.now() + 5_000;
        this.expected = Object.freeze(Object.create(null));
        this.clients = Object.freeze([]);
        this.publish({ invalidateContexts: true });
        throw error;
      }
      const { expected, clients } = authority;
      const registered = cloneRegisteredProviders(this.registered);
      let pruned = false;
      for (const prefix of Object.keys(registered)) {
        const definition = this.configuration.byPrefix[prefix];
        const authorized = expectedForDefinition(this.configuration, expected, prefix);
        if (!definition || !authorized || !registeredRoute(definition, authorized, registered[prefix])) {
          delete registered[prefix];
          pruned = true;
        }
      }
      for (const prefix of Object.keys(this.registrationLeases)) {
        if (!Object.hasOwn(registered, prefix)) delete this.registrationLeases[prefix];
      }
      this.expected = expected;
      this.clients = clients;
      this.registered = registered;
      this.authorityObservedIdentity = authority.identity;
      this.authorityReloadFailed = false;
      this.authorityRetryAt = 0;
      const snapshot = this.publish({ invalidateContexts: true });
      // Publish revocation in memory before the recovery-file optimization.
      // A disk error may fail the control request, but can never keep the old
      // capability active in this process.
      if (pruned) this.persistRegistered(registered);
      return snapshot;
    });
  }

  async refreshCatalog(signal = null) {
    const request = this.catalogRequest + 1;
    this.catalogRequest = request;
    const concrete = await gatewayCatalog(this.gatewayToken, { signal });
    return this.serialize(() => {
      if (signal?.aborted) {
        throw signal.reason instanceof Error
          ? signal.reason
          : new Error("catalog refresh was aborted before commit");
      }
      if (request < this.catalogApplied) return this.snapshot();
      this.catalogApplied = request;
      this.concrete = concrete;
      return this.publish();
    });
  }

  beginRegistrationRequest(prefix, now = Date.now()) {
    const last = this.registrationRequestAt[prefix] ?? Number.NEGATIVE_INFINITY;
    if (
      this.registrationRequests.has(prefix)
      || now - last < REGISTRATION_ATTEMPT_INTERVAL_MS
    ) {
      return null;
    }
    this.registrationRequestAt[prefix] = now;
    this.registrationRequests.add(prefix);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.registrationRequests.delete(prefix);
    };
  }

  renewRegistration({ prefix, registration, token, now = Date.now() }) {
    // This method is deliberately synchronous: one JavaScript turn is the
    // serialization barrier, while exact registration floods never enter the
    // shared mutation queue ahead of a capability revocation.
    const route = this.snapshot().components[prefix];
    if (
      !route
      || route.generation !== registration.generation
      || route.api !== registration.api
      || route.ingress_token !== token
      || JSON.stringify(route.models) !== JSON.stringify(registration.models)
    ) {
      return "changed";
    }
    try {
      requireProviderSocketIdentity(route.socket_path, route.socket_identity);
    } catch {
      return "changed";
    }
    const last = this.exactRegistrationAt[prefix] ?? Number.NEGATIVE_INFINITY;
    if (now - last < EXACT_REGISTRATION_INTERVAL_MS) {
      return "rate-limited";
    }
    // A successful exact re-registration is a lease barrier for dispatches
    // already using this route. It changes no durable state or route snapshot.
    this.exactRegistrationAt[prefix] = now;
    this.registrationLeases[prefix] = randomBytes(16).toString("hex");
    return "renewed";
  }

  beginChangedRegistration(prefix, now = Date.now()) {
    const last = this.changedRegistrationAt[prefix] ?? Number.NEGATIVE_INFINITY;
    if (
      this.changedRegistrations.has(prefix)
      || now - last < REGISTRATION_REWRITE_INTERVAL_MS
      || now - this.changedRegistrationGlobalAt
        < GLOBAL_REGISTRATION_REWRITE_INTERVAL_MS
    ) {
      return null;
    }
    this.changedRegistrations.add(prefix);
    // Reserve the byte/fsync budget before the health probe so new prefixes or
    // rewrites cannot queue back-to-back durable commits.
    this.changedRegistrationAt[prefix] = now;
    this.changedRegistrationGlobalAt = now;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.changedRegistrations.delete(prefix);
    };
  }

  registrationLease(route) {
    const current = this.registered[route.prefix];
    if (!this.registrationMatchesRoute(current, route)) return null;
    return this.registrationLeases[route.prefix] ?? null;
  }

  registrationMatchesRoute(current, route) {
    return Boolean(
      current
      && current.generation === route.generation
      && current.socket_identity === route.socket_identity
      && (
        route.registration_id !== null
          ? current.registration_id === route.registration_id
          : current.registration_id === undefined
            && current.registered_at === route.registered_at
      )
    );
  }

  async commitRegistration({ prefix, registration, token, socketIdentity }) {
    return this.serialize(async () => {
      const snapshot = this.snapshot();
      const authorized = expectedForDefinition(
        snapshot.configuration,
        snapshot.expected,
        prefix,
      );
      if (
        !authorized
        || authorized.generation !== registration.generation
        || !hashMatches(digestToken(token), authorized.token_sha256)
      ) {
        throw Object.assign(
          new Error("provider authorization changed during registration"),
          { statusCode: 403 },
        );
      }
      if (snapshot.collisions.has(prefix)) {
        throw Object.assign(
          new Error(`provider prefix collides with a concrete account: ${prefix}`),
          { statusCode: 409 },
        );
      }
      const definition = snapshot.configuration.byPrefix[prefix];
      if (!definition.inputs.every((reference) => catalogHasModel(snapshot.catalog, reference))) {
        throw Object.assign(
          new Error("provider inputs are not currently available in host.conf order"),
          { statusCode: 409 },
        );
      }
      requireProviderSocketIdentity(authorized.socket_path, socketIdentity);
      const previous = this.registered[prefix];
      if (
        previous?.generation === registration.generation
        && previous.api === registration.api
        && previous.socket_identity === socketIdentity
        && previous.ingress_token === token
        && JSON.stringify(previous.models) === JSON.stringify(registration.models)
      ) {
        this.registrationLeases[prefix] = randomBytes(16).toString("hex");
        return snapshot;
      }
      const registered = cloneRegisteredProviders(this.registered);
      registered[prefix] = {
        generation: registration.generation,
        api: registration.api,
        models: registration.models,
        ingress_token: token,
        registration_id: randomBytes(16).toString("hex"),
        socket_identity: socketIdentity,
        registered_at: new Date().toISOString(),
      };
      this.persistRegistered(registered);
      this.registered = registered;
      this.registrationLeases[prefix] = randomBytes(16).toString("hex");
      const committedAt = Date.now();
      this.changedRegistrationAt[prefix] = committedAt;
      this.changedRegistrationGlobalAt = committedAt;
      return this.publish();
    });
  }

  async dropComponent(route, observedLease) {
    return this.serialize(async () => {
      const current = this.registered[route.prefix];
      if (
        observedLease === null
        || this.registrationLeases[route.prefix] !== observedLease
        || !this.registrationMatchesRoute(current, route)
      ) {
        return false;
      }
      const registered = cloneRegisteredProviders(this.registered);
      delete registered[route.prefix];
      this.persistRegistered(registered);
      this.registered = registered;
      delete this.registrationLeases[route.prefix];
      this.publish();
      return true;
    });
  }
}


async function createRuntimeState() {
  const configuration = loadHostConfig();
  const authority = loadAuthorityFiles();
  const { expected, clients } = authority;
  const persisted = registeredProviderDocument().providers;
  const adminToken = readTokenFile(ADMIN_TOKEN_FILE, { optional: true });
  const gatewayToken = readTokenFile(GATEWAY_TOKEN_FILE);
  const concrete = await gatewayCatalog(gatewayToken);
  const registered = Object.create(null);
  const providerClients = indexProviderClients(clients);

  // Durable registrations are recovery hints, not the live route table. Probe
  // each eligible component once before publishing the startup snapshot.
  const candidates = [];
  for (const definition of configuration.definitions) {
    if (Object.hasOwn(concrete, definition.prefix)) continue;
    const authorized = expectedForDefinition(configuration, expected, definition.prefix);
    if (!authorized) continue;
    const providerClient = providerClients[definition.prefix];
    if (
      providerClient?.binding_generation !== authorized.generation
      || providerClient.enabled === false
      || providerClient.revoked === true
    ) {
      continue;
    }
    const route = registeredRoute(definition, authorized, persisted[definition.prefix]);
    if (!route) continue;
    candidates.push((async () => {
      try {
        await probeProviderHealth(authorized, route.socket_identity);
        registered[definition.prefix] = persisted[definition.prefix];
      } catch {
        // The component can register again when it is ready. Never make an
        // unavailable persisted endpoint part of the live startup snapshot.
      }
    })());
  }
  await Promise.all(candidates);
  return new RuntimeState({
    configuration,
    expected,
    clients,
    authorityIdentity: authority.identity,
    registered,
    concrete,
    adminToken,
    gatewayToken,
  });
}


function filteredCatalog(snapshot, principal) {
  const catalog = {};
  for (const [prefix, info] of Object.entries(snapshot.catalog)) {
    if (!principalAllowsProvider(principal, prefix)) continue;
    const models = info.models.filter(
      (model) => principalAllowsModel(principal, prefix, model.id),
    );
    if (!models.length && principal.kind !== "admin") continue;
    catalog[prefix] = {
      ...(info.kind === "component"
        ? {
          kind: "component",
          generation: info.generation,
          registered_at: info.registered_at,
        }
        : {}),
      api: info.api,
      models,
    };
  }
  return catalog;
}


function readBody(req, limit = MAX_REQUEST_BODY, timeoutMs = INBOUND_BODY_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const cleanup = () => {
      clearTimeout(timeout);
      req.off("data", onData);
      req.off("end", onEnd);
      req.off("error", onError);
      req.off("aborted", onAborted);
    };
    const fail = (error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
      req.destroy();
    };
    const onData = (chunk) => {
      size += chunk.length;
      if (size > limit) {
        fail(Object.assign(new Error("request body too large"), { statusCode: 413 }));
        return;
      }
      chunks.push(Buffer.from(chunk));
    };
    const onEnd = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(Buffer.concat(chunks, size));
    };
    const onError = (error) => fail(error);
    const onAborted = () => fail(new Error("request body was aborted"));
    const timeout = setTimeout(() => {
      fail(Object.assign(new Error("request body timed out"), { statusCode: 408 }));
    }, timeoutMs);
    timeout.unref?.();
    req.on("data", onData);
    req.on("end", onEnd);
    req.on("error", onError);
    req.on("aborted", onAborted);
  });
}


function sendPlain(res, status, text) {
  if (res.destroyed || res.writableEnded) return;
  const body = Buffer.from(text, "utf8");
  res.writeHead(status, {
    "content-type": "text/plain; charset=utf-8",
    "content-length": String(body.length),
  });
  res.end(body);
}


function sendJson(res, status, value) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": String(body.length),
  });
  res.end(body);
}


function incomingHeaders(headers) {
  return Object.freeze({
    *entries() {
      for (const [name, value] of Object.entries(headers)) {
        if (value !== undefined) yield [name, value];
      }
    },
    get(name) {
      const value = headers[String(name).toLowerCase()];
      return Array.isArray(value) ? value.join(", ") : value ?? null;
    },
  });
}


function providerSocketIdentity(socketPath) {
  let metadata;
  try {
    metadata = lstatSync(socketPath, { bigint: true, throwIfNoEntry: false });
  } catch (error) {
    throw new Error(`cannot inspect provider socket ${socketPath}: ${error.message}`);
  }
  if (!metadata || metadata.isSymbolicLink() || !metadata.isSocket()) {
    throw new Error(`provider endpoint is not a real Unix socket: ${socketPath}`);
  }
  return `${metadata.dev}:${metadata.ino}`;
}


function requireProviderSocketIdentity(socketPath, expected = null) {
  const current = providerSocketIdentity(socketPath);
  if (expected !== null && current !== expected) {
    throw new Error(`provider socket identity changed: ${socketPath}`);
  }
  return current;
}


function requestProviderSocket(socketPath, path, options = {}) {
  return new Promise((resolve, reject) => {
    let identity;
    try {
      identity = requireProviderSocketIdentity(
        socketPath,
        options.socketIdentity ?? null,
      );
    } catch (error) {
      reject(error);
      return;
    }
    const request = httpRequest({
      socketPath,
      path,
      method: options.method ?? "GET",
      headers: options.headers,
      signal: options.signal,
      agent: false,
    });
    request.once("response", (response) => {
      try {
        requireProviderSocketIdentity(socketPath, identity);
      } catch (error) {
        response.destroy(error);
        reject(error);
        return;
      }
      resolve({
        status: response.statusCode ?? 502,
        headers: incomingHeaders(response.headers),
        body: response,
      });
    });
    request.once("error", reject);
    request.end(options.body);
  });
}


function providerEndpointFailure(error) {
  return new Set(["ECONNREFUSED", "ECONNRESET", "ENOENT", "ENOTSOCK", "EPIPE"])
    .has(error?.code)
    || /^(?:cannot inspect provider socket|provider endpoint is not a real Unix socket|provider socket identity changed):?/u
      .test(error?.message ?? "");
}


async function probeProviderHealth(expected, pinnedIdentity = null) {
  const identity = requireProviderSocketIdentity(
    expected.socket_path,
    pinnedIdentity,
  );
  const response = await requestProviderSocket(expected.socket_path, "/health", {
    signal: AbortSignal.timeout(COMPONENT_HEALTH_TIMEOUT_MS),
    socketIdentity: identity,
  });
  if (response.status !== 200) throw new Error(`provider health check returned ${response.status}`);
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body ?? []) {
    const bytes = Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_COMPONENT_HEALTH_BODY) throw new Error("provider health response is too large");
    chunks.push(bytes);
  }
  if (!Buffer.concat(chunks, size).equals(Buffer.from("ok\n", "utf8"))) {
    throw new Error("provider health response must be exactly 'ok\\n'");
  }
  requireProviderSocketIdentity(expected.socket_path, identity);
  return identity;
}


function registrationDocument(body) {
  let document;
  try {
    document = JSON.parse(body.toString("utf8"));
  } catch {
    throw Object.assign(new Error("registration body must be valid JSON"), { statusCode: 400 });
  }
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw Object.assign(new Error("registration body must be a JSON object"), { statusCode: 400 });
  }
  const allowed = new Set(["version", "generation", "api", "models"]);
  if (Object.keys(document).some((key) => !allowed.has(key))) {
    throw Object.assign(new Error("registration body contains an unknown field"), { statusCode: 400 });
  }
  if (
    document.version !== 1
    || !safeGeneration(document.generation)
    || typeof document.api !== "string"
    || !API_NAME.test(document.api)
  ) {
    throw Object.assign(
      new Error("registration body has an invalid version, generation, or api"),
      { statusCode: 400 },
    );
  }
  try {
    return Object.freeze({
      version: 1,
      generation: document.generation,
      api: document.api,
      models: Object.freeze(sanitizedComponentModels(document.models)),
    });
  } catch (error) {
    if (!Number.isInteger(error?.statusCode)) error.statusCode = 400;
    throw error;
  }
}


function writeJsonAtomic(path, value) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.tmp.${process.pid}.${randomBytes(6).toString("hex")}`;
  const content = `${JSON.stringify(value, null, 2)}\n`;
  let descriptor = -1;
  try {
    descriptor = openSync(
      temporary,
      fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW,
      0o600,
    );
    writeFileSync(descriptor, content, "utf8");
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = -1;
    chmodSync(temporary, 0o600);
    renameSync(temporary, path);
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
    try {
      unlinkSync(temporary);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}


async function registerProvider(req, res, prefix, state) {
  if (req[REQUEST_TRANSPORT] !== TRANSPORT_PROVIDER_UDS) {
    sendPlain(res, 403, "provider registration requires the runtime Unix socket\n");
    return;
  }
  if (req[REQUEST_PROVIDER_PREFIX] !== prefix) {
    sendPlain(res, 403, "provider registration prefix does not match its Unix socket\n");
    return;
  }
  const initialSnapshot = state.snapshot();
  const authorized = expectedForDefinition(
    initialSnapshot.configuration,
    initialSnapshot.expected,
    prefix,
  );
  if (!authorized) {
    sendPlain(res, 403, "provider is not authorized by host.conf and expected state\n");
    return;
  }
  const releaseAttempt = state.beginRegistrationRequest(prefix);
  if (releaseAttempt === null) {
    res.shouldKeepAlive = false;
    sendPlain(res, 429, "provider registration request is already active or rate limited\n");
    return;
  }
  try {
    const token = presentedToken(req);
    if (!token || !hashMatches(digestToken(token), authorized.token_sha256)) {
      sendPlain(res, 401, "unauthorized\n");
      return;
    }
    const contentType = req.headers["content-type"];
  if (typeof contentType !== "string" || !/^application\/json(?:\s*;|$)/iu.test(contentType)) {
    sendPlain(res, 400, "registration content-type must be application/json\n");
    return;
  }
  let registration;
  try {
    registration = registrationDocument(
      await readBody(req, MAX_REGISTRATION_BODY),
    );
  } catch (error) {
    sendPlain(res, Number.isInteger(error?.statusCode) ? error.statusCode : 400, `${error.message}\n`);
    return;
  }
  if (registration.generation !== authorized.generation) {
    sendPlain(res, 403, "provider generation does not match expected state\n");
    return;
  }
  if (initialSnapshot.collisions.has(prefix)) {
    sendPlain(res, 409, `provider prefix collides with a concrete account: ${prefix}\n`);
    return;
  }
  if (!initialSnapshot.configuration.byPrefix[prefix].inputs.every(
    (reference) => catalogHasModel(initialSnapshot.catalog, reference),
  )) {
    sendPlain(res, 409, "provider inputs are not currently available in host.conf order\n");
    return;
  }
  const renewal = state.renewRegistration({ prefix, registration, token });
  if (renewal === "renewed") {
    res.writeHead(204);
    res.end();
    return;
  }
  if (renewal === "rate-limited") {
    sendPlain(res, 429, "provider registration is rate limited\n");
    return;
  }
  const releaseRegistration = state.beginChangedRegistration(prefix);
  if (releaseRegistration === null) {
    sendPlain(res, 429, "provider registration is already active or rate limited\n");
    return;
  }
  let socketIdentity;
  try {
    socketIdentity = await probeProviderHealth(authorized);
  } catch (error) {
    releaseRegistration();
    sendPlain(res, 502, `${error.message}\n`);
    return;
  }
  try {
    await state.commitRegistration({ prefix, registration, token, socketIdentity });
  } catch (error) {
    sendPlain(res, Number.isInteger(error?.statusCode) ? error.statusCode : 500, `${error.message}\n`);
    return;
  } finally {
    releaseRegistration();
  }
    res.writeHead(204);
    res.end();
  } finally {
    releaseAttempt();
  }
}


function providerClient(snapshot, prefix) {
  const expected = expectedForDefinition(snapshot.configuration, snapshot.expected, prefix);
  if (!expected) return null;
  const entry = snapshot.providerClients[prefix];
  if (
    entry?.binding_generation === expected.generation
    && entry.enabled !== false
    && entry.revoked !== true
  ) {
    return entry;
  }
  return null;
}


function liveProviderContext(req, principal, state, snapshot, now = Date.now()) {
  if (principal.kind !== "provider") return null;
  const token = req.headers[REQUEST_CONTEXT_HEADER];
  if (typeof token !== "string" || !token) return null;
  const context = state.requestContexts.get(token);
  const expected = expectedForDefinition(
    snapshot.configuration,
    snapshot.expected,
    principal.provider_prefix,
  );
  if (
    !context
    || !expected
    || context.expires_at <= now
    || context.policy_epoch !== snapshot.epoch
    || context.provider_prefix !== principal.provider_prefix
    || context.client_id !== principal.client_id
    || context.binding_generation !== principal.binding_generation
    || principal.binding_generation !== expected.generation
    || !Number.isInteger(context.budget?.remaining)
    || context.budget.remaining <= 0
  ) {
    if (context?.expires_at <= now) state.requestContexts.delete(token);
    return null;
  }
  return context;
}


function consumeProviderContext(req, principal, state, snapshot, now = Date.now()) {
  const context = liveProviderContext(req, principal, state, snapshot, now);
  if (!context) return null;
  context.budget.remaining -= 1;
  return context;
}


function issueProviderContext(state, snapshot, origin, parent, prefix) {
  const chain = [...(parent?.chain ?? []), prefix];
  if (chain.length > MAX_PROVIDER_CHAIN_DEPTH) return null;
  const client = providerClient(snapshot, prefix);
  if (!client) return null;
  const token = randomBytes(32).toString("base64url");
  state.requestContexts.set(token, {
    version: 1,
    origin_principal: parent?.origin_principal ?? origin.principal,
    origin_gateway_token: parent?.origin_gateway_token ?? origin.gatewayToken,
    chain,
    provider_prefix: prefix,
    client_id: client.client_id,
    binding_generation: client.binding_generation ?? null,
    budget: parent?.budget ?? { remaining: MAX_NESTED_PROVIDER_CALLS },
    expires_at: Date.now() + REQUEST_CONTEXT_TTL_MS,
    policy_epoch: snapshot.epoch,
  });
  return token;
}


function writeWithBackpressure(res, chunk) {
  if (!chunk.length) return Promise.resolve();
  if (res.destroyed || res.writableEnded) {
    return Promise.reject(new Error("downstream response is closed"));
  }
  if (res.write(chunk)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      res.off("drain", onDrain);
      res.off("close", onClose);
      res.off("error", onError);
    };
    const onDrain = () => { cleanup(); resolve(); };
    const onClose = () => { cleanup(); reject(new Error("downstream response closed")); };
    const onError = (error) => { cleanup(); reject(error); };
    res.once("drain", onDrain);
    res.once("close", onClose);
    res.once("error", onError);
  });
}


async function relayResponse(res, upstream, secrets = []) {
  const relayHeaders = Object.create(null);
  for (const [name, value] of upstream.headers.entries()) {
    if (!DROPPED_RESPONSE_HEADERS.has(name.toLowerCase())) relayHeaders[name] = value;
  }
  const headers = filterResponseHeaders(relayHeaders, secrets);
  const redactor = createResponseSecretRedactor(secrets);
  res.writeHead(upstream.status, headers);
  try {
    for await (const chunk of upstream.body ?? []) {
      await writeWithBackpressure(res, redactor.push(Buffer.from(chunk)));
    }
    await writeWithBackpressure(res, redactor.flush());
    res.end();
  } catch (error) {
    res.destroy(error);
  }
}


function requestAbort(req, res, timeoutMs) {
  const abort = new AbortController();
  const abortNow = () => {
    if (!abort.signal.aborted) abort.abort(new Error("downstream client disconnected"));
  };
  const timeout = setTimeout(() => {
    if (!abort.signal.aborted) abort.abort(new Error("provider runtime upstream request timed out"));
  }, timeoutMs);
  timeout.unref?.();
  const close = () => {
    if (!res.writableEnded) abortNow();
  };
  req.once("aborted", abortNow);
  res.once("close", close);
  return {
    signal: abort.signal,
    cleanup() {
      clearTimeout(timeout);
      req.off("aborted", abortNow);
      res.off("close", close);
    },
  };
}


async function handleProxy(
  req,
  res,
  state,
  principal,
  prefix,
  restPath,
  search,
  initialSnapshot,
  upstreamTimeoutMs,
  releaseBufferedRequest,
) {
  if (!isKnownInferenceEndpoint(req.method, restPath)) {
    sendPlain(res, 405, "only known POST inference endpoints are allowed\n");
    return;
  }
  let body;
  try {
    body = await readBody(req);
  } catch (error) {
    sendPlain(res, Number.isInteger(error?.statusCode) ? error.statusCode : 400, `${error.message}\n`);
    return;
  }
  let policyBody;
  let upstreamBody;
  try {
    ({ policyBody, upstreamBody } = await prepareRequestBody(
      body,
      req.headers["content-encoding"],
      MAX_REQUEST_BODY,
    ));
  } catch (error) {
    sendPlain(res, Number.isInteger(error?.statusCode) ? error.statusCode : 400, `${error.message}\n`);
    return;
  }
  if (state.snapshot().epoch !== initialSnapshot.epoch) {
    sendPlain(res, 503, "provider runtime policy changed; retry request\n");
    return;
  }
  const model = modelFromInferenceRequest(restPath, policyBody);
  if (!model) {
    sendPlain(res, 400, "inference request does not identify a model\n");
    return;
  }
  if (!principalAllowsModel(principal, prefix, model)) {
    sendPlain(res, 403, "model outside client scope\n");
    return;
  }
  const snapshot = initialSnapshot;
  const component = snapshot.components[prefix];
  const componentLease = component ? state.registrationLease(component) : null;
  const concrete = snapshot.concrete[prefix];
  if (!component && !concrete) {
    sendPlain(res, 404, `unknown provider: ${prefix}\n`);
    return;
  }

  let parent = null;
  let origin;
  if (principal.kind === "provider") {
    parent = consumeProviderContext(req, principal, state, snapshot);
    if (!parent) {
      sendPlain(res, 403, "provider inference requires a live request context\n");
      return;
    }
    const definition = snapshot.configuration.byPrefix[principal.provider_prefix];
    if (!definition?.inputs.includes(`${prefix}/${model}`)) {
      sendPlain(res, 403, "model outside host.conf provider inputs\n");
      return;
    }
    origin = {
      principal: parent.origin_principal,
      gatewayToken: parent.origin_gateway_token,
    };
  } else {
    origin = {
      principal,
      gatewayToken: principal.kind === "admin"
        ? state.gatewayToken
        : principal.token,
    };
  }

  const cancellation = requestAbort(req, res, upstreamTimeoutMs);
  const headers = forwardedRequestHeaders(req.headers);
  const currentProviderToken = principal.kind === "provider" ? principal.token : null;
  try {
    if (component) {
      const contextToken = issueProviderContext(state, snapshot, origin, parent, prefix);
      if (!contextToken) {
        sendPlain(res, 503, "provider upstream capability is unavailable\n");
        return;
      }
      headers.authorization = `Bearer ${component.ingress_token}`;
      headers[REQUEST_CONTEXT_HEADER] = contextToken;
      try {
        let upstream;
        try {
          upstream = await requestProviderSocket(
            component.socket_path,
            `${restPath}${search}`,
            {
              method: req.method,
              headers,
              body: upstreamBody,
              signal: cancellation.signal,
              socketIdentity: component.socket_identity,
            },
          );
          body = null;
          policyBody = null;
          upstreamBody = null;
          releaseBufferedRequest();
        } catch (error) {
          if (providerEndpointFailure(error)) {
            await state.dropComponent(component, componentLease);
          }
          throw error;
        }
        await relayResponse(res, upstream, [
          component.ingress_token,
          `Bearer ${component.ingress_token}`,
          contextToken,
          ...(currentProviderToken ? [currentProviderToken, `Bearer ${currentProviderToken}`] : []),
          origin.gatewayToken,
          `Bearer ${origin.gatewayToken}`,
        ]);
      } finally {
        state.requestContexts.delete(contextToken);
      }
      return;
    }

    headers.authorization = `Bearer ${origin.gatewayToken}`;
    const upstream = await fetch(`${GATEWAY_ORIGIN}/p/${prefix}${restPath}${search}`, {
      method: req.method,
      headers,
      body: upstreamBody,
      redirect: "manual",
      signal: cancellation.signal,
    });
    body = null;
    policyBody = null;
    upstreamBody = null;
    releaseBufferedRequest();
    const secrets = principal.kind === "provider"
      ? [
        origin.gatewayToken,
        `Bearer ${origin.gatewayToken}`,
        principal.token,
        `Bearer ${principal.token}`,
        req.headers[REQUEST_CONTEXT_HEADER],
      ].filter(Boolean)
      : [];
    await relayResponse(res, upstream, secrets);
  } catch (error) {
    if (!res.headersSent && !res.destroyed) {
      console.error(`provider runtime dispatch failed for ${prefix}: ${error.message}`);
      sendPlain(res, 502, "provider runtime upstream request failed\n");
    } else if (!res.destroyed) {
      res.destroy();
    }
  } finally {
    cancellation.cleanup();
  }
}


function normalizedIpAddress(value) {
  if (typeof value !== "string") return null;
  return value.startsWith("::ffff:") && isIP(value.slice(7)) === 4
    ? value.slice(7)
    : value;
}


function configureRuntimeServer(server, transport) {
  server.maxConnections = transport === TRANSPORT_TCP
    ? MAX_TCP_CONNECTIONS
    : transport === TRANSPORT_PROVIDER_UDS
      ? MAX_PROVIDER_SOCKET_CONNECTIONS
      : 32;
  server.headersTimeout = Math.min(10_000, INBOUND_BODY_TIMEOUT_MS);
  server.requestTimeout = INBOUND_BODY_TIMEOUT_MS;
  server.keepAliveTimeout = 5_000;
  server.maxRequestsPerSocket = 100;
  if (transport !== TRANSPORT_TCP) return server;

  // Every team reaches a different runtime-side address on its private Docker
  // network. Cap sockets per interface before HTTP headers (and therefore a
  // bearer) exist, so slow-header connections from one team cannot occupy the
  // entire shared listener.
  const connectionsByInterface = new Map();
  server.on("connection", (socket) => {
    const identity = normalizedIpAddress(socket.localAddress) ?? "unknown";
    const current = connectionsByInterface.get(identity) ?? 0;
    if (current >= MAX_TCP_CONNECTIONS_PER_INTERFACE) {
      socket.destroy();
      return;
    }
    connectionsByInterface.set(identity, current + 1);
    let released = false;
    socket.once("close", () => {
      if (released) return;
      released = true;
      const remaining = (connectionsByInterface.get(identity) ?? 1) - 1;
      if (remaining > 0) connectionsByInterface.set(identity, remaining);
      else connectionsByInterface.delete(identity);
    });
  });
  return server;
}


function createRuntimeServer({
  state,
  transport = TRANSPORT_TCP,
  providerPrefix = null,
  upstreamTimeoutMs = UPSTREAM_TIMEOUT_MS,
} = {}) {
  if (!(state instanceof RuntimeState)) {
    throw new TypeError("provider runtime server requires a shared RuntimeState");
  }
  if (![TRANSPORT_TCP, TRANSPORT_PROVIDER_UDS, TRANSPORT_ADMIN_UDS].includes(transport)) {
    throw new TypeError(`unsupported provider runtime transport: ${transport}`);
  }
  if (
    (transport === TRANSPORT_PROVIDER_UDS && !validRouteName(providerPrefix))
    || (transport !== TRANSPORT_PROVIDER_UDS && providerPrefix !== null)
  ) {
    throw new TypeError("provider Unix transport requires exactly one valid provider prefix");
  }
  if (!Number.isInteger(upstreamTimeoutMs) || upstreamTimeoutMs < 1) {
    throw new TypeError("provider runtime upstream timeout must be a positive integer");
  }
  const server = createServer((req, res) => {
    Object.defineProperty(req, REQUEST_TRANSPORT, { value: transport });
    Object.defineProperty(req, REQUEST_PROVIDER_PREFIX, { value: providerPrefix });
    (async () => {
      if (
        transport === TRANSPORT_TCP
        && !state.admitTcpTransport(req.socket?.localAddress)
      ) {
        res.shouldKeepAlive = false;
        sendPlain(res, 429, "team network request rate limit exceeded\n");
        return;
      }
      if (
        transport === TRANSPORT_PROVIDER_UDS
        && !state.admitProviderTransport(providerPrefix)
      ) {
        res.shouldKeepAlive = false;
        sendPlain(res, 429, "provider Unix transport rate limit exceeded\n");
        return;
      }
      const url = new URL(req.url ?? "/", "http://provider-runtime");
      if (req.method === "GET" && url.pathname === "/health") {
        res.setHeader(RUNTIME_BOOT_ID_HEADER, RUNTIME_BOOT_ID);
        sendPlain(res, 200, "ok\n");
        return;
      }
      const registration = url.pathname.match(/^\/_cyclo\/v1\/providers\/([a-z0-9_-]+)$/u);
      if (req.method === "PUT" && registration) {
        await registerProvider(req, res, registration[1], state);
        return;
      }
      const snapshot = state.snapshot();
      const principal = authenticate(req, state, snapshot);
      if (!principal) {
        sendPlain(res, 401, "unauthorized\n");
        return;
      }
      if (CONTROL_PATHS.has(url.pathname)) {
        if (transport !== TRANSPORT_ADMIN_UDS || principal.kind !== "admin") {
          sendPlain(res, 403, "runtime control requires an admin transport capability\n");
          return;
        }
        if (req.method !== "POST") {
          sendPlain(res, 405, "runtime control requires POST\n");
          return;
        }
        const body = await readBody(req);
        if (body.length !== 0) {
          sendPlain(res, 400, "runtime control request body must be empty\n");
          return;
        }
        if (url.pathname === CONTROL_RELOAD_PATH) {
          await state.reloadControl();
        } else {
          const cancellation = requestAbort(req, res, CATALOG_TIMEOUT_MS);
          try {
            await state.refreshCatalog(cancellation.signal);
          } finally {
            cancellation.cleanup();
          }
          if (res.destroyed || res.writableEnded) return;
        }
        res.writeHead(204);
        res.end();
        return;
      }
      const admissionContext = liveProviderContext(req, principal, state, snapshot);
      const providerCatalogRequest = (
        principal.kind === "provider"
        && req.method === "GET"
        && url.pathname === "/providers"
      );
      if (
        principal.kind === "provider"
        && !admissionContext
        && !providerCatalogRequest
      ) {
        // A provider capability is an input capability, not an independent
        // workload identity. Reject contextless inference before it can claim
        // either the project request pool or any request-body retention slot.
        res.shouldKeepAlive = false;
        sendPlain(res, 403, "provider inference requires a live request context\n");
        return;
      }
      const admission = admissionContext
        ? state.acquireNestedRequest(admissionContext.origin_principal)
        : state.acquireRequest(principal);
      if (admission.release === null) {
        res.shouldKeepAlive = false;
        sendPlain(
          res,
          admission.status,
          admission.status === 429
            ? "client request concurrency limit exceeded\n"
            : "provider runtime is at its request concurrency limit\n",
        );
        return;
      }
      try {
        if (req.method === "GET" && url.pathname === "/providers") {
          sendJson(res, 200, filteredCatalog(snapshot, principal));
          return;
        }
        const match = url.pathname.match(/^\/p\/([a-z0-9_-]+)(\/.*)?$/u);
        if (!match) {
          sendPlain(res, 404, "not found\n");
          return;
        }
        if (!principalAllowsProvider(principal, match[1])) {
          sendPlain(res, 403, "provider outside client scope\n");
          return;
        }
        const buffering = admissionContext
          ? state.acquireNestedBufferedRequest(admissionContext.origin_principal)
          : state.acquireBufferedRequest(principal);
        if (buffering.release === null) {
          res.shouldKeepAlive = false;
          sendPlain(
            res,
            buffering.status,
            buffering.status === 429
              ? "client request body concurrency limit exceeded\n"
              : "provider runtime is at its request body concurrency limit\n",
          );
          return;
        }
        try {
          await handleProxy(
            req,
            res,
            state,
            principal,
            match[1],
            match[2] ?? "/",
            url.search,
            snapshot,
            upstreamTimeoutMs,
            buffering.release,
          );
        } finally {
          buffering.release();
        }
      } finally {
        admission.release();
      }
    })().catch((error) => {
      console.error(`provider runtime request failed: ${error.message}`);
      if (!res.headersSent) sendPlain(res, 500, "provider runtime configuration error\n");
      else res.destroy();
    });
  });
  return configureRuntimeServer(server, transport);
}


function prepareRuntimeSocket(socketPath) {
  if (
    typeof socketPath !== "string"
    || !isAbsolute(socketPath)
    || normalize(socketPath) !== socketPath
    || Buffer.byteLength(socketPath) > 103
  ) {
    throw new Error("provider runtime socket must be a normalized absolute path of at most 103 bytes");
  }
  const parent = lstatSync(dirname(socketPath), { throwIfNoEntry: false });
  if (!parent?.isDirectory() || parent.isSymbolicLink()) {
    throw new Error(`provider runtime socket parent is not a real directory: ${dirname(socketPath)}`);
  }
  const existing = lstatSync(socketPath, { throwIfNoEntry: false });
  if (existing) {
    if (!existing.isSocket() || existing.isSymbolicLink()) {
      throw new Error(`refusing to replace non-socket runtime path: ${socketPath}`);
    }
    unlinkSync(socketPath);
  }
}


function prepareProviderRuntimeSocket(prefix) {
  const root = lstatSync(RUNTIME_SOCKET_ROOT, { throwIfNoEntry: false });
  if (!root?.isDirectory() || root.isSymbolicLink()) {
    throw new Error(`provider runtime socket root is not a real directory: ${RUNTIME_SOCKET_ROOT}`);
  }
  const socketPath = providerRuntimeSocketPath(prefix);
  const directory = dirname(socketPath);
  const existing = lstatSync(directory, { throwIfNoEntry: false });
  if (!existing) mkdirSync(directory, { mode: 0o777 });
  const prepared = lstatSync(directory, { throwIfNoEntry: false });
  if (!prepared?.isDirectory() || prepared.isSymbolicLink()) {
    throw new Error(`provider runtime socket directory is not real: ${directory}`);
  }
  chmodSync(directory, 0o777);
  return socketPath;
}


function listenRuntimeSocket(server, socketPath, onListening, mode = 0o666) {
  prepareRuntimeSocket(socketPath);
  server.listen(socketPath, () => {
    chmodSync(socketPath, mode);
    const opened = lstatSync(socketPath);
    server.once("close", () => {
      const current = lstatSync(socketPath, { throwIfNoEntry: false });
      if (
        current?.isSocket()
        && !current.isSymbolicLink()
        && current.dev === opened.dev
        && current.ino === opened.ino
      ) {
        unlinkSync(socketPath);
      }
    });
    onListening?.();
  });
}


async function main() {
  if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
    throw new Error("CYCLO_PROVIDER_RUNTIME_PORT must be from 1 to 65535");
  }
  // Build one validated startup snapshot before either listener reports
  // healthy. host.conf is immutable for this process; applying any edit is an
  // explicit `cyclo runtime restart` operation.
  const state = await createRuntimeState();
  state.startAuthorityWatch();
  const tcp = createRuntimeServer({ state, transport: TRANSPORT_TCP });
  const admin = createRuntimeServer({ state, transport: TRANSPORT_ADMIN_UDS });
  const providerServers = state.snapshot().configuration.definitions.map((definition) => ({
    prefix: definition.prefix,
    socketPath: prepareProviderRuntimeSocket(definition.prefix),
    server: createRuntimeServer({
      state,
      transport: TRANSPORT_PROVIDER_UDS,
      providerPrefix: definition.prefix,
    }),
  }));
  const servers = [tcp, admin, ...providerServers.map((item) => item.server)];
  await Promise.all([
    new Promise((resolve, reject) => {
      admin.once("error", reject);
      listenRuntimeSocket(admin, ADMIN_SOCKET, () => {
        admin.off("error", reject);
        console.log(`cyclo-provider-runtime transport=admin-unix listening on ${ADMIN_SOCKET}`);
        resolve();
      }, 0o600);
    }),
    ...providerServers.map((item) => new Promise((resolve, reject) => {
      item.server.once("error", reject);
      listenRuntimeSocket(item.server, item.socketPath, () => {
        item.server.off("error", reject);
        console.log(
          `cyclo-provider-runtime transport=provider-unix prefix=${item.prefix} `
          + `listening on ${item.socketPath}`,
        );
        resolve();
      });
    })),
  ]);
  tcp.listen(PORT, "0.0.0.0", () => {
    console.log(`cyclo-provider-runtime transport=tcp listening on :${PORT}`);
  });
  let closing = false;
  const close = () => {
    if (closing) return;
    closing = true;
    state.stopAuthorityWatch();
    let remaining = servers.length;
    const complete = () => {
      remaining -= 1;
      if (remaining === 0) process.exit(0);
    };
    for (const server of servers) server.close(complete);
  };
  process.on("SIGTERM", close);
  process.on("SIGINT", close);
}


if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`provider runtime startup failed: ${error.message}`);
    process.exit(1);
  });
}


export {
  TRANSPORT_ADMIN_UDS,
  TRANSPORT_PROVIDER_UDS,
  TRANSPORT_TCP,
  createRuntimeState,
  createRuntimeServer,
  filteredCatalog,
  listenRuntimeSocket,
  loadHostConfig,
  providerRuntimeSocketPath,
};
