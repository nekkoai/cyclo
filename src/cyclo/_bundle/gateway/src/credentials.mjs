import { readJson, withFileLock, writeJsonAtomic } from "./store.mjs";

const REFRESH_SKEW_MS = 60_000;

function storeDocument(path) {
  const value = readJson(path);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`credential store ${path} must be a JSON object`);
  }
  return value;
}

function routeIdentity(route) {
  if (
    !route
    || typeof route !== "object"
    || typeof route.account !== "string"
    || !route.account
    || typeof route.provider !== "string"
    || !route.provider
  ) {
    throw new TypeError("credential route requires account and provider");
  }
  return { account: route.account, provider: route.provider };
}

function credentialFor(store, account, provider) {
  const credential = store[account];
  if (!credential || typeof credential !== "object" || Array.isArray(credential)) {
    throw new Error(`no credential for account ${account}`);
  }
  if ((credential.provider ?? account) !== provider) {
    throw new Error(`credential ${account} no longer belongs to provider ${provider}`);
  }
  return credential;
}

function notExpired(credential) {
  return Date.now() + REFRESH_SKEW_MS < credential.expires;
}

function validateOAuth(credential, account) {
  if (
    credential.type !== "oauth"
    || typeof credential.access !== "string"
    || !credential.access
    || typeof credential.refresh !== "string"
    || !credential.refresh
    || typeof credential.expires !== "number"
    || !Number.isFinite(credential.expires)
    || credential.expires <= 0
  ) {
    throw new Error(`credential ${account} is not a complete OAuth credential`);
  }
  return credential;
}

function accountIdFromAccessToken(access) {
  try {
    const [, payload] = String(access).split(".");
    const json = Buffer.from(
      payload.replace(/-/gu, "+").replace(/_/gu, "/"),
      "base64",
    ).toString("utf8");
    const claim = JSON.parse(json)["https://api.openai.com/auth"];
    return typeof claim?.chatgpt_account_id === "string" && claim.chatgpt_account_id
      ? claim.chatgpt_account_id
      : null;
  } catch {
    return null;
  }
}

function sensitiveValues(apiKey, credential, model) {
  const values = new Set();
  const add = (value, bearer = false) => {
    if (typeof value !== "string" || !value) return;
    values.add(value);
    if (bearer) values.add(`Bearer ${value}`);
  };
  add(apiKey, true);
  add(credential.key, true);
  add(credential.access, true);
  add(credential.refresh);
  add(credential.accountId);
  if (model?.headers && typeof model.headers === "object") {
    for (const value of Object.values(model.headers)) add(value);
  }
  return Object.freeze([...values]);
}

function credentialModel(oauth, route, credential) {
  if (!route.rawModel || typeof oauth?.modifyModels !== "function") return route.rawModel;
  const sourceModel = route.sourceModel ?? route.rawModel;
  const modified = oauth.modifyModels([structuredClone(sourceModel)], credential);
  if (!Array.isArray(modified) || modified.length !== 1) {
    throw new Error(`credential ${route.account} does not enable model ${route.rawModel.id}`);
  }
  const model = modified[0];
  if (
    !model
    || typeof model !== "object"
    || model.id !== route.rawModel.id
    || model.provider !== route.rawModel.provider
    || model.api !== route.rawModel.api
    || typeof model.baseUrl !== "string"
  ) {
    throw new Error(`credential ${route.account} produced an invalid model route`);
  }
  let url;
  try {
    url = new URL(model.baseUrl);
  } catch {
    throw new Error(`credential ${route.account} produced an invalid model URL`);
  }
  if (
    !["http:", "https:"].includes(url.protocol)
    || url.username
    || url.password
    || url.search
    || url.hash
  ) {
    throw new Error(`credential ${route.account} produced an unsafe model URL`);
  }
  return Object.freeze(structuredClone({ ...model, baseUrl: url.toString() }));
}

export function createCredentialResolver({ authPath, getOAuthProvider }) {
  if (typeof authPath !== "string" || !authPath) {
    throw new TypeError("authPath must be a non-empty path");
  }
  if (typeof getOAuthProvider !== "function") {
    throw new TypeError("getOAuthProvider must be a function");
  }
  const refreshInFlight = new Map();

  async function freshOAuth(account, provider, initial) {
    if (notExpired(initial)) return initial;
    const key = `${account}\u0000${provider}`;
    if (!refreshInFlight.has(key)) {
      const refresh = withFileLock(authPath, async () => {
        const store = storeDocument(authPath);
        const current = credentialFor(store, account, provider);
        validateOAuth(current, account);
        if (notExpired(current)) return current;
        const oauth = getOAuthProvider(provider);
        if (!oauth || typeof oauth.refreshToken !== "function" || typeof oauth.getApiKey !== "function") {
          throw new Error(`no OAuth implementation for provider ${provider}`);
        }
        const updated = await oauth.refreshToken(current);
        if (!updated || typeof updated !== "object" || Array.isArray(updated)) {
          throw new Error(`provider ${provider} returned an invalid refreshed credential`);
        }
        const accountId = updated.accountId
          ?? current.accountId
          ?? accountIdFromAccessToken(updated.access);
        const next = {
          ...current,
          ...updated,
          type: "oauth",
          provider,
          ...(accountId ? { accountId } : {}),
        };
        try {
          validateOAuth(next, account);
        } catch {
          throw new Error(`provider ${provider} returned an invalid refreshed credential`);
        }
        store[account] = next;
        writeJsonAtomic(authPath, store);
        return store[account];
      });
      refreshInFlight.set(key, refresh.finally(() => refreshInFlight.delete(key)));
    }
    return refreshInFlight.get(key);
  }

  async function resolve(route) {
    const { account, provider } = routeIdentity(route);
    let credential = credentialFor(storeDocument(authPath), account, provider);
    let apiKey;
    let oauth;
    if (credential.type === "api_key") {
      if (typeof credential.key !== "string" || !credential.key) {
        throw new Error(`credential ${account} has no API key`);
      }
      apiKey = credential.key;
    } else if (credential.type === "oauth") {
      validateOAuth(credential, account);
      credential = await freshOAuth(account, provider, credential);
      oauth = getOAuthProvider(provider);
      if (!oauth || typeof oauth.getApiKey !== "function") {
        throw new Error(`no OAuth implementation for provider ${provider}`);
      }
      apiKey = oauth.getApiKey(credential);
      if (apiKey && typeof apiKey.then === "function") apiKey = await apiKey;
      if (typeof apiKey !== "string" || !apiKey) {
        throw new Error(`provider ${provider} produced no API key`);
      }
    } else {
      throw new Error(`credential ${account} has an invalid type`);
    }
    const effectiveModel = credentialModel(oauth, route, credential);
    return Object.freeze({
      apiKey,
      sensitiveValues: sensitiveValues(apiKey, credential, effectiveModel),
      effectiveModel,
    });
  }

  function check(routes) {
    if (routes.length === 0) return;
    const store = storeDocument(authPath);
    const accounts = new Set();
    for (const route of routes) {
      if (accounts.has(route.account)) continue;
      accounts.add(route.account);
      const credential = credentialFor(store, route.account, route.provider);
      if (
        credential.type === "api_key"
        && (typeof credential.key !== "string" || !credential.key)
      ) {
        throw new Error(`credential ${route.account} has no API key`);
      }
      if (credential.type === "oauth") {
        validateOAuth(credential, route.account);
        const oauth = getOAuthProvider(route.provider);
        if (!oauth || typeof oauth.refreshToken !== "function" || typeof oauth.getApiKey !== "function") {
          throw new Error(`no OAuth implementation for provider ${route.provider}`);
        }
      }
      if (!new Set(["api_key", "oauth"]).has(credential.type)) {
        throw new Error(`credential ${route.account} has an invalid type`);
      }
    }
  }

  return Object.freeze({ resolve, check });
}
