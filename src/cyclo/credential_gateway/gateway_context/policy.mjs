// Pure authorization helpers for the credential boundary.  Keep this module
// dependency-free so the policy can be exercised without a gateway image.

const SIMPLE_INFERENCE_PATHS = new Set([
  "/chat/completions",
  "/v1/chat/completions",
  "/responses",
  "/v1/responses",
  "/codex/responses",
  "/v1/codex/responses",
  "/messages",
  "/v1/messages",
]);

const GOOGLE_INFERENCE_ACTIONS = ["generateContent", "streamGenerateContent"];
const GOOGLE_INTERNAL_PATHS = new Set(
  GOOGLE_INFERENCE_ACTIONS.flatMap((action) => [
    `/v1internal:${action}`,
    `/v1internal/${action}`,
  ]),
);
const GOOGLE_MODEL_PATH = new RegExp(
  `^/(?:v1/|v1beta/)?models/([^/]+):(${GOOGLE_INFERENCE_ACTIONS.join("|")})$`,
  "u",
);
const GOOGLE_VERTEX_MODEL_PATH = new RegExp(
  `^/v1/projects/[^/]+/locations/[^/]+/publishers/google/models/([^/]+):(${GOOGLE_INFERENCE_ACTIONS.join("|")})$`,
  "u",
);

function normalizedPath(path) {
  if (typeof path !== "string" || !path.startsWith("/")) return null;
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

export function modelFromGooglePath(path) {
  const normalized = normalizedPath(path);
  if (!normalized) return null;
  const match = normalized.match(GOOGLE_MODEL_PATH)
    ?? normalized.match(GOOGLE_VERTEX_MODEL_PATH);
  if (!match) return null;
  try {
    const model = decodeURIComponent(match[1]);
    return model
      && !/[\\\s\u0000-\u001f\u007f]/u.test(model)
      && !model.split("/").some((segment) => segment === "." || segment === "..")
      ? model
      : null;
  } catch {
    return null;
  }
}

export function isKnownInferenceEndpoint(method, path) {
  if (method !== "POST") return false;
  const normalized = normalizedPath(path);
  if (!normalized) return false;
  return (
    SIMPLE_INFERENCE_PATHS.has(normalized) ||
    GOOGLE_INTERNAL_PATHS.has(normalized) ||
    modelFromGooglePath(normalized) !== null
  );
}

function topLevelObjectKeys(text) {
  const keys = [];
  let offset = 0;
  const whitespace = /\s/u;
  const skipWhitespace = () => {
    while (offset < text.length && whitespace.test(text[offset])) offset += 1;
  };
  skipWhitespace();
  if (text[offset] !== "{") return null;
  offset += 1;
  skipWhitespace();
  if (text[offset] === "}") return keys;

  while (offset < text.length) {
    skipWhitespace();
    if (text[offset] !== '"') return null;
    const keyStart = offset;
    offset += 1;
    let escaped = false;
    while (offset < text.length) {
      const character = text[offset];
      offset += 1;
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === '"') {
        break;
      }
    }
    keys.push(JSON.parse(text.slice(keyStart, offset)));
    skipWhitespace();
    if (text[offset] !== ":") return null;
    offset += 1;

    let depth = 0;
    let inString = false;
    escaped = false;
    for (; offset < text.length; offset += 1) {
      const character = text[offset];
      if (inString) {
        if (escaped) escaped = false;
        else if (character === "\\") escaped = true;
        else if (character === '"') inString = false;
        continue;
      }
      if (character === '"') inString = true;
      else if (character === "{" || character === "[") depth += 1;
      else if (character === "}" || character === "]") {
        if (depth > 0) depth -= 1;
        else break;
      } else if (character === "," && depth === 0) {
        break;
      }
    }
    if (text[offset] === ",") {
      offset += 1;
      continue;
    }
    if (text[offset] === "}") return keys;
    return null;
  }
  return null;
}

export function modelFromInferenceRequest(path, body) {
  const pathModel = modelFromGooglePath(path);
  if (pathModel) return pathModel;
  if (!body || body.length === 0) return null;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.from(body));
    const parsed = JSON.parse(text);
    const keys = topLevelObjectKeys(text);
    if (
      !parsed
      || typeof parsed !== "object"
      || Array.isArray(parsed)
      || !keys
      || keys.filter((key) => key === "model").length !== 1
    ) {
      return null;
    }
    return typeof parsed.model === "string" && parsed.model ? parsed.model : null;
  } catch {
    return null;
  }
}

export function principalAllowsModel(principal, provider, model) {
  if (principal?.kind === "admin") return true;
  if (typeof provider !== "string" || typeof model !== "string" || !model) return false;
  const models = Array.isArray(principal?.models) ? principal.models : [];
  return models.includes(`${provider}/${model}`);
}

export function clientPrincipalFromRegistryEntry(entry) {
  return {
    kind: "client",
    client_id: entry.client_id,
    team_id: typeof entry.team_id === "string" ? entry.team_id : entry.client_id,
    binding_generation:
      typeof entry.binding_generation === "string" && entry.binding_generation
        ? entry.binding_generation
        : null,
    providers: Array.isArray(entry.providers)
      ? entry.providers.filter((value) => typeof value === "string")
      : [],
    models: Array.isArray(entry.models)
      ? entry.models.filter((value) => typeof value === "string")
      : [],
  };
}

export function filterModelsForPrincipal(principal, provider, models) {
  return models.filter(
    (model) => model && principalAllowsModel(principal, provider, model.id),
  );
}
