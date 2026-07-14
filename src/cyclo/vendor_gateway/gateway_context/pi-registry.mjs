// Keep pi-ai's generated provider catalog behind one small compatibility
// boundary.  Since pi-ai 0.80 the static catalog lives at providers/all;
// the package root intentionally no longer exports getModels/getProviders.

import {
  getBuiltinModels as readBuiltinModels,
  getBuiltinProviders as readBuiltinProviders,
} from "@earendil-works/pi-ai/providers/all";

function getBuiltinProviders() {
  const providers = readBuiltinProviders();
  if (
    !Array.isArray(providers) ||
    providers.length === 0 ||
    providers.some((provider) => typeof provider !== "string" || !provider) ||
    new Set(providers).size !== providers.length
  ) {
    throw new Error("pi-ai returned an invalid built-in provider registry");
  }
  return providers;
}

function getBuiltinModels(provider) {
  if (typeof provider !== "string" || !provider) {
    throw new TypeError("provider must be a non-empty string");
  }
  const models = readBuiltinModels(provider);
  if (!Array.isArray(models)) {
    throw new Error(`pi-ai returned an invalid model registry for ${provider}`);
  }
  return models;
}

// Used by the credential-free image smoke.  This deliberately walks every
// built-in provider so a stale/missing pi-ai API or incompatible catalog shape
// fails before a gateway is started with real credentials.
function checkBuiltinRegistry() {
  const providers = getBuiltinProviders();
  let modelCount = 0;
  for (const provider of providers) {
    const models = getBuiltinModels(provider);
    if (models.length === 0) {
      throw new Error(`pi-ai returned no built-in models for ${provider}`);
    }
    for (const model of models) {
      if (
        !model ||
        typeof model.id !== "string" ||
        !model.id ||
        typeof model.baseUrl !== "string" ||
        typeof model.api !== "string" ||
        !model.api
      ) {
        throw new Error(`pi-ai returned an invalid built-in model for ${provider}`);
      }
    }
    modelCount += models.length;
  }
  return { providerCount: providers.length, modelCount };
}

export { checkBuiltinRegistry, getBuiltinModels, getBuiltinProviders };
