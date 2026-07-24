import {
  isLocalModelId,
  isProviderPrefix,
  PI_INFERENCE_FORMAT,
} from "@cyclo/provider/protocol";

import { getPiProviders, validatePiProviders } from "./pi-registry.mjs";
import { readJson } from "./store.mjs";

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
  if (!isProviderPrefix(value)) {
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
  } catch {
    throw new Error(`provider ${provider} has an invalid baseUrl`);
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
  return credentialAccountsDocument(value, `credential store ${authPath}`);
}

function credentialAccountsDocument(value, label) {
  const document = objectDocument(value, label);
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
  let clone;
  try {
    clone = structuredClone(value);
  } catch {
    throw new Error("model metadata cannot be cloned");
  }
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
  if (
    !isLocalModelId(model.id)
  ) {
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

function effectiveModels(provider, rawModels, defaults, { custom = false, reject }) {
  const models = [];
  const ids = new Set();
  for (const raw of rawModels) {
    let model;
    try {
      model = effectiveModel(provider, raw, defaults, { custom });
    } catch (error) {
      reject(raw?.id, error);
      continue;
    }
    if (ids.has(model.id)) {
      reject(model.id, new Error(`provider ${provider} repeats model ${model.id}`));
      continue;
    }
    ids.add(model.id);
    models.push(model);
  }
  return models;
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
  return Object.freeze({
    id: `${account}/${model.id}`,
    displayName: typeof model.name === "string" && model.name ? model.name : model.id,
    capabilities,
    extensions: Object.freeze([]),
    inferenceFormat: PI_INFERENCE_FORMAT,
    contextWindowTokens: BigInt(tokenLimit(model.contextWindow, "context window", account, model.id)),
    maxOutputTokens: BigInt(tokenLimit(model.maxTokens, "output limit", account, model.id)),
  });
}

function tokenLimit(value, label, account, model) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`model ${account}/${model} has no usable ${label}`);
  }
  return value;
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
  return buildCatalogueFromAccounts({
    accounts: credentialAccounts(authPath),
    custom: customProviders(modelsPath),
    providers,
    getApiProvider,
  });
}

// Login validates an in-memory candidate document through exactly the same
// catalogue path used at gateway startup. The live credential file is not
// changed unless this succeeds.
export function buildCatalogueForCredentials({
  credentials,
  modelsPath,
  providers = getPiProviders(),
  getApiProvider,
}) {
  if (typeof modelsPath !== "string" || !modelsPath) {
    throw new TypeError("modelsPath is required");
  }
  if (typeof getApiProvider !== "function") {
    throw new TypeError("getApiProvider is required");
  }
  return buildCatalogueFromAccounts({
    accounts: credentialAccountsDocument(
      structuredClone(credentials),
      "candidate credential store",
    ),
    custom: customProviders(modelsPath),
    providers,
    getApiProvider,
  });
}

function buildCatalogueFromAccounts({
  accounts,
  custom,
  providers,
  getApiProvider,
}) {
  const builtins = new Map(validatePiProviders(providers).map((provider) => [
    provider.id,
    provider,
  ]));
  const models = [];
  const routes = Object.create(null);
  const diagnostics = [];

  for (const { account, provider, credential } of accounts) {
    const reject = (model, error) => {
      diagnostics.push(diagnostic(account, model, error));
    };
    let rawModels;
    let defaults = {};
    let customModels = false;
    if (Object.hasOwn(custom, provider)) {
      ({ models: rawModels, ...defaults } = custom[provider]);
      customModels = true;
    } else if (builtins.has(provider)) {
      const builtin = builtins.get(provider);
      try {
        rawModels = builtin.getModels();
      } catch {
        reject(
          undefined,
          new Error(`provider ${provider} model discovery failed`),
        );
        continue;
      }
      if (!Array.isArray(rawModels)) {
        reject(
          undefined,
          new Error(`built-in provider ${provider} returned a malformed model list`),
        );
        continue;
      }
      defaults = { baseUrl: builtin.baseUrl };
    } else {
      throw new Error(`account ${account} names unknown provider ${provider}`);
    }

    let candidates = effectiveModels(provider, rawModels, defaults, {
      custom: customModels,
      reject,
    });
    const originals = new Map(candidates.map((model) => [model.id, model.api]));
    const filterModels = builtins.get(provider)?.filterModels;
    if (credential.type === "oauth" && typeof filterModels === "function") {
      let modified;
      try {
        modified = filterModels(
          candidates.map((model) => structuredClone(model)),
          credential,
        );
      } catch {
        reject(
          undefined,
          new Error(`provider ${provider} OAuth model filtering failed`),
        );
        continue;
      }
      if (!Array.isArray(modified)) {
        reject(
          undefined,
          new Error(`provider ${provider} returned a malformed filtered model list`),
        );
        continue;
      }
      candidates = effectiveModels(provider, modified, defaults, { reject });
      const changed = candidates.find((model) => originals.get(model.id) !== model.api);
      if (changed) {
        reject(
          changed.id,
          new Error(`provider ${provider} changed a model identity while filtering`),
        );
        continue;
      }
    }

    for (const model of candidates) {
      if (!EXPOSED_APIS.has(model.api)) continue;
      let apiProvider;
      try {
        apiProvider = getApiProvider(model.api);
      } catch {
        reject(
          model.id,
          new Error(`model ${provider}/${model.id} API lookup failed`),
        );
        continue;
      }
      if (
        !apiProvider
        || apiProvider.api !== model.api
        || typeof apiProvider.stream !== "function"
        || typeof apiProvider.streamSimple !== "function"
      ) {
        reject(
          model.id,
          new Error(
            `model ${provider}/${model.id} has no API implementation for ${model.api}`,
          ),
        );
        continue;
      }
      let published;
      try {
        published = publicModel(account, model);
      } catch (error) {
        reject(model.id, error);
        continue;
      }
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
    diagnostics: Object.freeze(diagnostics),
  });
}

function diagnostic(account, model, error) {
  const raw = error instanceof Error ? error.message : String(error);
  const message = raw
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, 512) || "invalid catalogue entry";
  return Object.freeze({
    account,
    ...(typeof model === "string" && model ? { model } : {}),
    message,
  });
}
