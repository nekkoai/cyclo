// List accounts provisioned in the gateway's own credential store.
// Used by `cyclo gateway status`.

import { readJson } from "./store.mjs";
import { checkBuiltinRegistry } from "./pi-registry.mjs";

checkBuiltinRegistry();

const path = process.env.CYCLO_GATEWAY_AUTH_JSON ?? "/var/lib/cyclo-gateway/auth.json";
const store = readJson(path) ?? {};
const entries = Object.entries(store);

if (!entries.length) {
  console.log("(no accounts provisioned — run `cyclo gateway providers` to list login choices)");
} else {
  console.log("ACCOUNT\tPROVIDER\tCREDENTIAL");
  for (const [account, cred] of entries) {
    const obj = cred && typeof cred === "object" ? cred : {};
    console.log(`${account}\t${obj.provider ?? account}\t${obj.type ?? "?"}`);
  }
}
