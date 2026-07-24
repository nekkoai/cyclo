import { builtinProviders } from "@earendil-works/pi-ai/providers/all";
import { isProviderPrefix } from "@cyclo/provider/protocol";

export function validatePiProviders(providers) {
  if (!Array.isArray(providers) || providers.length === 0) {
    throw new Error("pi-ai returned an invalid provider registry");
  }
  const ids = new Set();
  for (const provider of providers) {
    if (
      !provider
      || typeof provider !== "object"
      || !isProviderPrefix(provider.id)
      || ids.has(provider.id)
      || !provider.auth
      || typeof provider.auth !== "object"
      || typeof provider.getModels !== "function"
      || typeof provider.streamSimple !== "function"
    ) {
      throw new Error("pi-ai returned an invalid provider registry");
    }
    ids.add(provider.id);
  }
  return Object.freeze([...providers]);
}

const BUILTIN_PROVIDERS = validatePiProviders(builtinProviders());
const BUILTIN_BY_ID = new Map(BUILTIN_PROVIDERS.map((provider) => [provider.id, provider]));

export function getPiProviders() {
  return BUILTIN_PROVIDERS;
}

export function getPiProvider(id) {
  return BUILTIN_BY_ID.get(id);
}
