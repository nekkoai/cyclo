import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { Writable } from "node:stream";
import { pathToFileURL } from "node:url";

import { getApiProvider } from "@earendil-works/pi-ai/compat";
import { isProviderPrefix } from "@cyclo/provider/protocol";

import { buildCatalogueForCredentials } from "./catalogue.mjs";
import { createAuthInteraction } from "./oauth-ui.mjs";
import { getPiProvider, getPiProviders } from "./pi-registry.mjs";
import { readJson, withFileLock, writeJsonAtomic } from "./store.mjs";

export async function login(argv, options = {}) {
  const env = options.env ?? process.env;
  const input = options.input ?? stdin;
  const output = options.output ?? stdout;
  const providerLookup = options.getProvider ?? getPiProvider;
  const providers = options.providers ?? getPiProviders();
  const apiProviderLookup = options.getApiProvider ?? getApiProvider;
  const parsed = parseLoginArgs(argv);
  const key = await resolveApiKey(parsed, { env, input, output });
  const account = parsed.account ?? parsed.provider;
  const credential = key === undefined
    ? await oauthLogin(parsed.provider, { input, output, providerLookup, providers })
    : { type: "api_key", key };
  const authPath = env.CYCLO_GATEWAY_AUTH_JSON ?? "/var/lib/cyclo-gateway/auth.json";
  const modelsPath = env.CYCLO_GATEWAY_MODELS_JSON ?? "/etc/cyclo-gateway/models.json";
  const validateStore = options.validateStore ?? ((candidate) => {
    const catalogue = buildCatalogueForCredentials({
      credentials: candidate,
      modelsPath,
      providers,
      getApiProvider: apiProviderLookup,
    });
    const usable = Object.values(catalogue.routes).some(
      (route) => route.account === account,
    );
    if (!usable) {
      throw new Error(
        `provider ${parsed.provider} exposes no usable models for account ${account}`,
      );
    }
  });
  await storeCredential(authPath, account, {
    ...credential,
    provider: parsed.provider,
  }, validateStore);
  output.write(`stored ${credential.type} credential for ${account}\n`);
}

export function parseLoginArgs(argv) {
  const provider = routeName(argv[0], "provider name");
  const result = {
    provider,
    account: undefined,
    apiKeyEnv: undefined,
    apiKeyStdin: false,
  };
  for (let index = 1; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === "--as") result.account = routeName(requireValue(argv, ++index, flag), "account name");
    else if (flag === "--api-key-env") result.apiKeyEnv = requireValue(argv, ++index, flag);
    else if (flag === "--api-key-stdin") result.apiKeyStdin = true;
    else throw new Error(`unknown argument: ${flag}`);
  }
  return result;
}

async function storeCredential(path, account, credential, validate) {
  routeName(account, "account name");
  if (typeof validate !== "function") {
    throw new TypeError("credential validator must be a function");
  }
  await withFileLock(path, async () => {
    const store = readJson(path) ?? {};
    if (!store || typeof store !== "object" || Array.isArray(store)) {
      throw new Error("credential store must be a JSON object");
    }
    const candidate = structuredClone(store);
    candidate[account] = structuredClone(credential);
    await validate(structuredClone(candidate));
    writeJsonAtomic(path, candidate);
  });
}

async function resolveApiKey(parsed, { env, input, output }) {
  const sources = [
    parsed.apiKeyEnv !== undefined,
    parsed.apiKeyStdin,
  ].filter(Boolean).length;
  if (sources > 1) throw new Error("use exactly one API-key source");
  if (parsed.apiKeyEnv !== undefined) {
    const value = env[parsed.apiKeyEnv];
    if (!value) throw new Error(`environment variable ${parsed.apiKeyEnv} is empty or unset`);
    return value;
  }
  if (!parsed.apiKeyStdin) return undefined;
  if (input.isTTY) return hiddenQuestion(input, output, "Enter API key (hidden): ");
  const chunks = [];
  for await (const chunk of input) chunks.push(Buffer.from(chunk));
  const value = Buffer.concat(chunks).toString("utf8").trim();
  if (!value) throw new Error("no API key on stdin");
  return value;
}

async function oauthLogin(provider, { input, output, providerLookup, providers }) {
  const selected = providerLookup(provider);
  const oauth = selected?.auth?.oauth;
  if (!oauth) {
    if (selected) {
      throw new Error(`provider ${provider} requires an API key`);
    }
    const choices = providers.filter((item) => item.auth?.oauth).map(({ id }) => id).join(", ");
    throw new Error(`unknown provider ${provider}; OAuth providers: ${choices}`);
  }
  const credential = await oauth.login(createAuthInteraction({
    ask: (question) => visibleQuestion(input, output, question),
    askSecret: (question) => hiddenQuestion(input, output, question),
    write: (message) => output.write(`${message}\n`),
  }));
  return { ...credential, type: "oauth" };
}

async function visibleQuestion(input, output, prompt) {
  const terminal = createInterface({ input, output });
  try {
    return await terminal.question(prompt);
  } finally {
    terminal.close();
  }
}

async function hiddenQuestion(input, output, prompt) {
  output.write(prompt);
  const muted = new Writable({
    write(_chunk, _encoding, callback) { callback(); },
  });
  const terminal = createInterface({ input, output: muted, terminal: true });
  try {
    const value = (await terminal.question("")).trim();
    if (!value) throw new Error("no API key entered");
    return value;
  } finally {
    terminal.close();
    output.write("\n");
  }
}

function routeName(value, label) {
  if (!isProviderPrefix(value)) {
    throw new Error(
      `${label} must start with a lowercase letter or number and use at most `
      + "64 lowercase letters, numbers, underscores, or hyphens",
    );
  }
  return value;
}

function requireValue(argv, index, flag) {
  const value = argv[index];
  if (value === undefined || value.startsWith("-")) throw new Error(`${flag} requires a value`);
  return value;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  login(process.argv.slice(2)).catch((error) => {
    console.error(`login failed: ${error.message}`);
    process.exitCode = 1;
  });
}
