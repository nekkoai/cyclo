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

function normalizedPath(path) {
  if (typeof path !== "string" || !path.startsWith("/")) return null;
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

export function modelFromGooglePath(path) {
  const normalized = normalizedPath(path);
  if (!normalized) return null;
  const marker = "/models/";
  const markerIndex = normalized.lastIndexOf(marker);
  if (markerIndex < 0) return null;
  for (const action of GOOGLE_INFERENCE_ACTIONS) {
    const suffix = `:${action}`;
    if (!normalized.endsWith(suffix)) continue;
    const encoded = normalized.slice(markerIndex + marker.length, -suffix.length);
    if (!encoded) return null;
    try {
      const model = decodeURIComponent(encoded);
      return model && !/[\u0000-\u001f\u007f]/u.test(model) ? model : null;
    } catch {
      return null;
    }
  }
  return null;
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

export function modelFromInferenceRequest(path, body) {
  const pathModel = modelFromGooglePath(path);
  if (pathModel) return pathModel;
  if (!body || body.length === 0) return null;
  try {
    const parsed = JSON.parse(Buffer.from(body).toString("utf8"));
    return typeof parsed?.model === "string" && parsed.model ? parsed.model : null;
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
