import { createClient } from "@connectrpc/connect";
import { createDockerTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";

import { groupModels, streamProvider } from "./adapter.mjs";

const CATALOGUE_TIMEOUT_MS = 10_000;

export default async function cycloProviderExtension(pi) {
  return registerCycloProviders(pi, {
    target: process.env.DCOMP_LINK_PROVIDER,
  });
}

export async function registerCycloProviders(pi, {
  target,
  client = providerClient(target),
  catalogueTimeoutMs = CATALOGUE_TIMEOUT_MS,
  warn = console.warn,
} = {}) {
  if (!pi || typeof pi.registerProvider !== "function") {
    throw new TypeError("Pi registerProvider API is required");
  }
  if (!Number.isSafeInteger(catalogueTimeoutMs) || catalogueTimeoutMs <= 0) {
    throw new TypeError("catalogueTimeoutMs must be a positive integer");
  }
  if (typeof warn !== "function") {
    throw new TypeError("warn must be a function");
  }

  const catalogue = await client.listModels({}, {
    signal: AbortSignal.timeout(catalogueTimeoutMs),
  });
  const groups = groupModels(catalogue.models, {
    onInvalid(message) {
      warn(`Cyclo ignored an unusable provider model: ${message}`);
    },
  });
  if (groups.size === 0 && catalogue.models?.length) {
    throw new TypeError("Provider catalogue contains no usable Pi models");
  }
  const routes = new Map();
  for (const [provider, group] of groups) {
    for (const route of group) routes.set(routeKey(provider, route.model.id), route);
  }
  const streamSimple = (model, context, options) => {
    const route = routes.get(routeKey(model?.provider, model?.id));
    if (!route) throw new Error("Pi selected a model outside the Cyclo catalogue");
    return streamProvider(client, route.publicId, {
      api: "cyclo-pi",
      provider: model.provider,
      id: model.id,
      maxTokens: route.model.maxTokens,
    }, context, options);
  };

  for (const [provider, group] of groups) {
    pi.registerProvider(provider, {
      name: `Cyclo ${provider}`,
      api: "cyclo-pi",
      baseUrl: "http://cyclo.invalid",
      apiKey: "dcomp",
      authHeader: false,
      models: group.map(({ model }) => model),
      streamSimple,
    });
  }

  return groups.size;
}

export function providerClient(target) {
  return createClient(Provider, createDockerTransport(target));
}

function routeKey(provider, model) {
  return `${provider}\u0000${model}`;
}
