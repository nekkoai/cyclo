// Provision a credential into the gateway's own private store.
//
//   node login.mjs <provider>                  OAuth device/PKCE flow
//   node login.mjs <provider> --api-key KEY    static API key (argv -- see note)
//   node login.mjs <provider> --api-key-env V  read the key from env var V
//   node login.mjs <provider> --api-key-stdin  read the key from stdin
//
// Prefer --api-key-env or --api-key-stdin: a key passed as --api-key lands in
// shell history and the process argument list (visible via `ps` to other host
// users while the container runs). The env/stdin forms keep the secret out of
// argv. Exactly one key source may be given; none means OAuth.
//
// For OAuth this creates the gateway's OWN session (its own refresh-token
// chain), independent of the host pi's session, so the gateway is the sole
// writer of its store and credential rotation never races. Run once per
// provider; the store persists in the gateway volume.

import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { Writable } from "node:stream";
import { pathToFileURL } from "node:url";
import { getOAuthProvider, getOAuthProviders } from "@earendil-works/pi-ai/oauth";
import { getBuiltinProviders } from "./pi-registry.mjs";
import { createOAuthLoginCallbacks } from "./oauth-ui.mjs";
import { readJson, withFileLock, writeJsonAtomic } from "./store.mjs";

const AUTH_JSON_PATH = process.env.CYCLO_GATEWAY_AUTH_JSON ?? "/var/lib/cyclo-gateway/auth.json";

// Common brand names that don't match pi-ai's provider id. Used only to make
// error/warning messages helpful; never silently rewrites the provider.
const BRAND_ALIASES = { grok: "xai" };

function aliasHint(provider) {
  const alias = BRAND_ALIASES[provider];
  return alias ? ` Did you mean '${alias}'? (e.g. login.mjs ${alias} --api-key <key>)` : "";
}

function requireValue(argv, i, flag) {
  const value = argv[i];
  if (value === undefined || value.startsWith("-")) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function parseArgs(argv) {
  const provider = argv[0];
  if (!provider || provider.startsWith("-")) {
    throw new Error("usage: login.mjs <provider> [--as ACCOUNT] [--api-key KEY | --api-key-env VAR | --api-key-stdin]");
  }
  const parsed = { provider, account: undefined, apiKey: undefined, apiKeyEnv: undefined, apiKeyStdin: false };
  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--as") parsed.account = requireValue(argv, ++i, arg);
    else if (arg === "--api-key") parsed.apiKey = requireValue(argv, ++i, arg);
    else if (arg === "--api-key-env") parsed.apiKeyEnv = requireValue(argv, ++i, arg);
    else if (arg === "--api-key-stdin") parsed.apiKeyStdin = true;
    else throw new Error(`unknown argument: ${arg}`);
  }
  return parsed;
}

// Read one secret from a TTY without echoing it. The prompt is written first,
// then keystroke echo is muted, so the typed key never appears on screen.
async function questionHidden(query) {
  const muted = new Writable({
    write(chunk, encoding, callback) {
      if (!muted.muted) stdout.write(chunk, encoding);
      callback();
    },
  });
  muted.muted = false;
  stdout.write(query);
  const rl = createInterface({ input: stdin, output: muted, terminal: true });
  muted.muted = true;
  try {
    return await rl.question("");
  } finally {
    rl.close();
    stdout.write("\n");
  }
}

async function readKeyFromStdin() {
  if (stdin.isTTY) {
    const key = (await questionHidden("Enter API key (hidden): ")).trim();
    if (!key) throw new Error("no API key entered");
    return key;
  }
  const chunks = [];
  for await (const chunk of stdin) chunks.push(chunk);
  const key = Buffer.concat(chunks).toString("utf8").trim();
  if (!key) throw new Error("no API key on stdin");
  return key;
}

// Resolve the API key from exactly one source, or undefined to mean OAuth.
async function resolveApiKey({ apiKey, apiKeyEnv, apiKeyStdin }) {
  const chosen = [
    apiKey !== undefined ? "--api-key" : null,
    apiKeyEnv !== undefined ? "--api-key-env" : null,
    apiKeyStdin ? "--api-key-stdin" : null,
  ].filter(Boolean);
  if (chosen.length > 1) {
    throw new Error(`use only one key source, got: ${chosen.join(", ")}`);
  }
  if (apiKey !== undefined) return apiKey;
  if (apiKeyEnv !== undefined) {
    const key = process.env[apiKeyEnv];
    if (!key) throw new Error(`environment variable '${apiKeyEnv}' is empty or unset`);
    return key;
  }
  if (apiKeyStdin) return readKeyFromStdin();
  return undefined;
}

async function storeCredential(account, credential) {
  await withFileLock(AUTH_JSON_PATH, () => {
    const store = readJson(AUTH_JSON_PATH) ?? {};
    store[account] = credential;
    writeJsonAtomic(AUTH_JSON_PATH, store);
  });
  const label = account === credential.provider ? account : `${account} (${credential.provider})`;
  console.log(`stored ${credential.type} credential for ${label} in ${AUTH_JSON_PATH}`);
}

async function loginOAuth(provider) {
  const oauth = getOAuthProvider(provider);
  if (!oauth) {
    // No OAuth flow for this provider. If pi-ai knows it as a built-in (xai,
    // openai, mistral, ...) it's API-key based; tell the user how. Otherwise
    // it's unknown (or a custom provider that must be provisioned with a key).
    if (getBuiltinProviders().includes(provider)) {
      throw new Error(
        `provider '${provider}' authenticates with an API key, not OAuth.\n` +
          `Provision it with:  login.mjs ${provider} --api-key <key>`,
      );
    }
    const oauthIds = getOAuthProviders().map((p) => p.id).join(", ");
    throw new Error(
      `unknown provider '${provider}'.${aliasHint(provider)}\n` +
        `OAuth providers: ${oauthIds}. Any other built-in or custom provider is ` +
        `API-key based -- pass --api-key <key>.`,
    );
  }
  const rl = createInterface({ input: stdin, output: stdout });
  try {
    const credentials = await oauth.login(
      createOAuthLoginCallbacks({
        ask: (question) => rl.question(question),
      }),
    );
    return { ...credentials, type: "oauth" };
  } finally {
    rl.close();
  }
}

async function main() {
  const parsed = parseArgs(process.argv.slice(2));
  const key = await resolveApiKey(parsed);
  if (key !== undefined && !getBuiltinProviders().includes(parsed.provider)) {
    // Not a built-in id: only served if the host pi defines a custom provider
    // of this exact name. A brand-name typo (grok -> xai) lands here.
    console.warn(
      `warning: '${parsed.provider}' is not a built-in provider.${aliasHint(parsed.provider)}\n` +
        `It will only be served if your host pi's models.json defines a provider named '${parsed.provider}'.`,
    );
  }
  // The account (store key) defaults to the provider id; pass --as to hold
  // several accounts of the same provider (e.g. two anthropic logins). The
  // credential records its provider TYPE so the gateway can resolve it.
  const account = parsed.account || parsed.provider;
  const base =
    key !== undefined
      ? { type: "api_key", key }
      : await loginOAuth(parsed.provider);
  await storeCredential(account, { ...base, provider: parsed.provider });
  return 0;
}

// Run only when invoked directly, so tests can import the pure helpers.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then(
    (code) => process.exit(code),
    (exc) => {
      console.error(`login failed: ${exc.message}`);
      process.exit(1);
    },
  );
}

export { parseArgs, resolveApiKey, storeCredential };
