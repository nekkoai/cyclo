import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { getPiProviders, validatePiProviders } from "./pi-registry.mjs";
import { readJson } from "./store.mjs";

const ACCOUNT_NAME = /^[a-z0-9_-]+$/u;
const MODEL_ID = /^[^\s\u0000-\u001f\u007f]+$/u;
const RESERVED_ACCOUNTS = new Set(["__proto__", "constructor", "gateway", "prototype"]);
export const EXPOSED_APIS = new Set([
  "anthropic-messages",
  "openai-codex-responses",
  "openai-responses",
]);
const TEXT = 1;
const IMAGE = 2;

function objectDocument(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value;
}

function routeName(value, label) {
  if (typeof value !== "string" || !ACCOUNT_NAME.test(value) || RESERVED_ACCOUNTS.has(value)) {
    throw new Error(`${label} is not a valid route name`);
  }
  return value;
}

function providerUrl(provider, value) {
  if (typeof value !== "string" || !value) {
    throw new Error(`provider ${provider} has no baseUrl`);
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (error) {
    throw new Error(`provider ${provider} has an invalid baseUrl: ${error.message}`, {
      cause: error,
    });
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error(`provider ${provider} baseUrl must use http or https`);
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(`provider ${provider} baseUrl must not contain credentials, query, or fragment`);
  }
  return parsed.toString();
}

function customProviders(modelsPath) {
  const document = readJson(modelsPath);
  if (document === null) return Object.freeze(Object.create(null));
  objectDocument(document, `provider configuration ${modelsPath}`);
  if (document.providers === undefined) return Object.freeze(Object.create(null));
  const providers = objectDocument(document.providers, `providers in ${modelsPath}`);
  const clean = Object.create(null);
  for (const [name, raw] of Object.entries(providers)) {
    routeName(name, "custom provider name");
    const provider = objectDocument(raw, `custom provider ${name}`);
    if (!Array.isArray(provider.models)) {
      throw new Error(`custom provider ${name} requires a models array`);
    }
    if (provider.api !== undefined && (typeof provider.api !== "string" || !provider.api)) {
      throw new Error(`custom provider ${name} has an invalid API`);
    }
    const api = provider.api;
    const baseUrl = provider.baseUrl === undefined
      ? undefined
      : providerUrl(name, provider.baseUrl);
    clean[name] = Object.freeze({ api, baseUrl, models: provider.models });
  }
  return Object.freeze(clean);
}

function credentialAccounts(authPath) {
  const value = readJson(authPath);
  if (value === null) return Object.freeze([]);
  const document = objectDocument(value, `credential store ${authPath}`);
  const accounts = [];
  for (const [account, raw] of Object.entries(document)) {
    routeName(account, "account name");
    const credential = objectDocument(raw, `credential ${account}`);
    if (!new Set(["api_key", "oauth"]).has(credential.type)) {
      throw new Error(`credential ${account} has an invalid type`);
    }
    if (credential.type === "api_key" && (typeof credential.key !== "string" || !credential.key)) {
      throw new Error(`credential ${account} has no API key`);
    }
    if (
      credential.type === "oauth"
      && (typeof credential.access !== "string"
        || !credential.access
        || typeof credential.refresh !== "string"
        || !credential.refresh
        || typeof credential.expires !== "number"
        || !Number.isFinite(credential.expires)
        || credential.expires <= 0)
    ) {
      throw new Error(`credential ${account} is not a complete OAuth credential`);
    }
    const provider = routeName(credential.provider ?? account, `provider for account ${account}`);
    accounts.push(Object.freeze({ account, provider, credential }));
  }
  return Object.freeze(accounts);
}

function cloneAndFreeze(value) {
  if (!value || typeof value !== "object") return value;
  const clone = structuredClone(value);
  const freeze = (current) => {
    if (!current || typeof current !== "object" || Object.isFrozen(current)) return current;
    for (const child of Object.values(current)) freeze(child);
    return Object.freeze(current);
  };
  return freeze(clone);
}

function effectiveModel(provider, raw, defaults, { custom = false } = {}) {
  const model = objectDocument(raw, `model for provider ${provider}`);
  if (custom && Object.hasOwn(model, "headers")) {
    throw new Error(`custom model ${provider}/${model.id ?? "?"} must not define headers`);
  }
  if (typeof model.id !== "string" || !MODEL_ID.test(model.id)) {
    throw new Error(`provider ${provider} has an invalid model id`);
  }
  if (
    !Array.isArray(model.input)
    || model.input.some((modality) => !["text", "image"].includes(modality))
  ) {
    throw new Error(`model ${provider}/${model.id} has invalid input modalities`);
  }
  if (model.compat !== undefined && (!model.compat || typeof model.compat !== "object" || Array.isArray(model.compat))) {
    throw new Error(`model ${provider}/${model.id} has invalid compatibility metadata`);
  }
  const api = model.api ?? defaults.api;
  const baseUrl = providerUrl(provider, model.baseUrl ?? defaults.baseUrl);
  if (typeof api !== "string" || !api) {
    throw new Error(`model ${provider}/${model.id} has no API`);
  }
  return cloneAndFreeze({ ...model, provider, api, baseUrl });
}

function publicModel(account, model) {
  const capabilities = Object.freeze({
    inputModalities: Object.freeze(model.input.includes("image") ? [TEXT, IMAGE] : [TEXT]),
    outputModalities: Object.freeze([TEXT]),
    functionTools: true,
    parallelToolCalls: true,
    reasoningSummaries: false,
    temperature: model.reasoning !== true && model.compat?.supportsTemperature !== false,
    topP: false,
    stopSequences: false,
    extensionTypes: Object.freeze([]),
    reasoning: model.reasoning === true,
  });
  const result = {
    id: `${account}/${model.id}`,
    displayName: typeof model.name === "string" && model.name ? model.name : model.id,
    capabilities,
    extensions: Object.freeze([]),
    inferenceFormat: PI_INFERENCE_FORMAT,
  };
  if (Number.isSafeInteger(model.contextWindow) && model.contextWindow > 0) {
    result.contextWindowTokens = BigInt(model.contextWindow);
  }
  if (Number.isSafeInteger(model.maxTokens) && model.maxTokens > 0) {
    result.maxOutputTokens = BigInt(model.maxTokens);
  }
  return Object.freeze(result);
}

export function buildCatalogue({
  authPath,
  modelsPath,
  providers = getPiProviders(),
  getApiProvider,
}) {
  for (const [name, value] of Object.entries({
    authPath,
    modelsPath,
    getApiProvider,
  })) {
    const valid = name.endsWith("Path") ? typeof value === "string" && value : typeof value === "function";
    if (!valid) throw new TypeError(`${name} is required`);
  }
  const custom = customProviders(modelsPath);
  const builtins = new Map(
    validatePiProviders(providers).map((provider) => [provider.id, provider]),
  );
  const models = [];
  const routes = Object.create(null);

  for (const { account, provider, credential } of credentialAccounts(authPath)) {
    let rawModels;
    let defaults = {};
    let customModels = false;
    if (Object.hasOwn(custom, provider)) {
      ({ models: rawModels, ...defaults } = custom[provider]);
      customModels = true;
    } else if (builtins.has(provider)) {
      const builtin = builtins.get(provider);
      rawModels = builtin.getModels();
      if (!Array.isArray(rawModels)) {
        throw new Error(`built-in provider ${provider} returned a malformed model list`);
      }
      defaults = { baseUrl: builtin.baseUrl };
    } else {
      throw new Error(`account ${account} names unknown provider ${provider}`);
    }

    let candidates = rawModels.map((raw) => effectiveModel(
      provider,
      raw,
      defaults,
      { custom: customModels },
    ));
    const sourceIds = new Set();
    for (const model of candidates) {
      if (sourceIds.has(model.id)) throw new Error(`provider ${provider} repeats model ${model.id}`);
      sourceIds.add(model.id);
    }
    const originals = new Map(candidates.map((model) => [model.id, model.api]));
    const filterModels = builtins.get(provider)?.filterModels;
    if (credential.type === "oauth" && typeof filterModels === "function") {
      const modified = filterModels(
        candidates.map((model) => structuredClone(model)),
        credential,
      );
      if (!Array.isArray(modified)) {
        throw new Error(`provider ${provider} returned a malformed filtered model list`);
      }
      candidates = modified.map((raw) => effectiveModel(provider, raw, defaults));
      if (candidates.some((model) => originals.get(model.id) !== model.api)) {
        throw new Error(`provider ${provider} changed a model identity while filtering`);
      }
    }

    const seen = new Set();
    for (const model of candidates) {
      if (seen.has(model.id)) throw new Error(`provider ${provider} repeats model ${model.id}`);
      seen.add(model.id);
      if (!EXPOSED_APIS.has(model.api)) continue;
      const apiProvider = getApiProvider(model.api);
      if (
        !apiProvider
        || apiProvider.api !== model.api
        || typeof apiProvider.stream !== "function"
        || typeof apiProvider.streamSimple !== "function"
      ) {
        throw new Error(`model ${provider}/${model.id} has no API implementation for ${model.api}`);
      }
      const published = publicModel(account, model);
      if (Object.hasOwn(routes, published.id)) {
        throw new Error(`duplicate public model id ${published.id}`);
      }
      models.push(published);
      routes[published.id] = Object.freeze({
        account,
        provider,
        api: model.api,
        baseUrl: model.baseUrl,
        publicModel: published,
        rawModel: model,
        apiProvider,
      });
    }
  }

  models.sort((left, right) => left.id.localeCompare(right.id));
  return Object.freeze({
    models: Object.freeze(models),
    routes: Object.freeze(routes),
  });
}
