import { routeName } from "./route-name.mjs";
import { readJson, withFileLock, writeJsonAtomic } from "./store.mjs";

const DEFAULT_AUTH_PATH = "/var/lib/cyclo-gateway/auth.json";

export async function logout(argv, options = {}) {
  if (argv.length !== 1) throw new Error("usage: logout ACCOUNT");
  const account = routeName(argv[0], "account name");
  const env = options.env ?? process.env;
  const output = options.output ?? process.stdout;
  await updateCredentialStore(authPath(env), (candidate) => {
    if (!Object.hasOwn(candidate, account)) {
      throw new Error(`account ${account} is not stored`);
    }
    delete candidate[account];
  });
  output.write(`removed stored credential for ${account}\n`);
}

export async function rename(argv, options = {}) {
  if (argv.length !== 2) throw new Error("usage: rename OLD_ACCOUNT NEW_ACCOUNT");
  const source = routeName(argv[0], "source account name");
  const target = routeName(argv[1], "target account name");
  if (source === target) throw new Error("source and target account names must differ");
  const env = options.env ?? process.env;
  const output = options.output ?? process.stdout;
  await updateCredentialStore(authPath(env), (candidate) => {
    if (!Object.hasOwn(candidate, source)) {
      throw new Error(`account ${source} is not stored`);
    }
    if (Object.hasOwn(candidate, target)) {
      throw new Error(`account ${target} is already stored`);
    }
    const credential = candidate[source];
    if (!credential || typeof credential !== "object" || Array.isArray(credential)) {
      throw new Error(`credential ${source} must be a JSON object`);
    }
    const moved = structuredClone(credential);
    // Legacy records inferred the provider from the account key. Preserve that
    // identity before changing the key.
    if (moved.provider == null) moved.provider = source;
    candidate[target] = moved;
    delete candidate[source];
  });
  output.write(`renamed stored credential ${source} to ${target}\n`);
}

function authPath(env) {
  return env.CYCLO_GATEWAY_AUTH_JSON ?? DEFAULT_AUTH_PATH;
}

async function updateCredentialStore(path, update) {
  await withFileLock(path, async () => {
    const store = readJson(path) ?? {};
    if (!store || typeof store !== "object" || Array.isArray(store)) {
      throw new Error("credential store must be a JSON object");
    }
    const candidate = structuredClone(store);
    update(candidate);
    writeJsonAtomic(path, candidate);
  });
}
