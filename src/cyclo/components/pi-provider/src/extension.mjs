import { createClient } from "@connectrpc/connect";
import { createUnixTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";

import { groupModels, streamProvider } from "./adapter.mjs";

const CATALOGUE_TIMEOUT_MS = 10_000;

export default async function cycloProviderExtension(pi) {
  return registerCycloProviders(pi, {
    socketPath: process.env.CYCLO_PROVIDER_SOCKET,
  });
}

export async function registerCycloProviders(pi, {
  socketPath,
  client = providerClient(socketPath),
  catalogueTimeoutMs = CATALOGUE_TIMEOUT_MS,
} = {}) {
  if (!pi || typeof pi.registerProvider !== "function") {
    throw new TypeError("Pi registerProvider API is required");
  }
  if (!Number.isSafeInteger(catalogueTimeoutMs) || catalogueTimeoutMs <= 0) {
    throw new TypeError("catalogueTimeoutMs must be a positive integer");
  }

  const catalogue = await client.listModels({}, {
    signal: AbortSignal.timeout(catalogueTimeoutMs),
  });
  const groups = groupModels(catalogue.models);
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
      apiKey: "socket",
      authHeader: false,
      models: group.map(({ model }) => model),
      streamSimple,
    });
  }

  return groups.size;
}

export function providerClient(socketPath) {
  return createClient(Provider, createUnixTransport(socketPath));
}

function routeKey(provider, model) {
  return `${provider}\u0000${model}`;
}
