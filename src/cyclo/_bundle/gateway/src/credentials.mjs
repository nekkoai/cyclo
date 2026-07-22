import { readJson, withFileLock, writeJsonAtomic } from "./store.mjs";
import { getPiProvider } from "./pi-registry.mjs";

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

function safeProviderUrl(value, provider) {
  if (typeof value !== "string" || !value) {
    throw new Error(`provider ${provider} produced an invalid model URL`);
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`provider ${provider} produced an invalid model URL`);
  }
  if (
    !["http:", "https:"].includes(url.protocol)
    || url.username
    || url.password
    || url.search
    || url.hash
  ) {
    throw new Error(`provider ${provider} produced an unsafe model URL`);
  }
  return url.toString();
}

function oauthImplementation(providerLookup, provider) {
  const oauth = providerLookup(provider)?.auth?.oauth;
  if (
    !oauth
    || typeof oauth.login !== "function"
    || typeof oauth.refresh !== "function"
    || typeof oauth.toAuth !== "function"
  ) {
    throw new Error(`no OAuth implementation for provider ${provider}`);
  }
  return oauth;
}

function authenticatedModel(model, auth, provider) {
  if (!auth || typeof auth !== "object" || Array.isArray(auth)) {
    throw new Error(`provider ${provider} produced invalid request authentication`);
  }
  if (auth.apiKey !== undefined && (typeof auth.apiKey !== "string" || !auth.apiKey)) {
    throw new Error(`provider ${provider} produced an invalid API key`);
  }
  if (auth.baseUrl === undefined && auth.headers === undefined) return undefined;
  if (!model || typeof model !== "object" || Array.isArray(model)) {
    throw new Error(`provider ${provider} has no native model route`);
  }
  let headers;
  if (auth.headers !== undefined) {
    if (
      !auth.headers
      || typeof auth.headers !== "object"
      || Array.isArray(auth.headers)
      || Object.entries(auth.headers).some(
        ([key, value]) => !key || (typeof value !== "string" && value !== null),
      )
    ) {
      throw new Error(`provider ${provider} produced invalid request headers`);
    }
    headers = { ...(model.headers ?? {}), ...auth.headers };
  }
  const baseUrl = auth.baseUrl === undefined
    ? model.baseUrl
    : safeProviderUrl(auth.baseUrl, provider);
  return Object.freeze(structuredClone({
    ...model,
    ...(baseUrl === undefined ? {} : { baseUrl }),
    ...(headers === undefined ? {} : { headers }),
  }));
}

export function createCredentialResolver({ authPath, getProvider = getPiProvider }) {
  if (typeof authPath !== "string" || !authPath) {
    throw new TypeError("authPath must be a non-empty path");
  }
  if (typeof getProvider !== "function") {
    throw new TypeError("getProvider must be a function");
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
        const oauth = oauthImplementation(getProvider, provider);
        const updated = await oauth.refresh(current);
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
      oauth = oauthImplementation(getProvider, provider);
      const auth = await oauth.toAuth(credential);
      const effectiveModel = authenticatedModel(route.rawModel, auth, provider);
      apiKey = auth.apiKey;
      if (typeof apiKey !== "string" || !apiKey) {
        throw new Error(`provider ${provider} produced no API key`);
      }
      return Object.freeze({
        apiKey,
        ...(effectiveModel === undefined ? {} : { effectiveModel }),
      });
    } else {
      throw new Error(`credential ${account} has an invalid type`);
    }
    return Object.freeze({ apiKey });
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
        oauthImplementation(getProvider, route.provider);
      }
      if (!new Set(["api_key", "oauth"]).has(credential.type)) {
        throw new Error(`credential ${route.account} has an invalid type`);
      }
    }
  }

  return Object.freeze({ resolve, check });
}
