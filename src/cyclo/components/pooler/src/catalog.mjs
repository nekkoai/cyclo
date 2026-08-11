import { isDeepStrictEqual } from "node:util";

import {
  outputModelId,
  providerPrefix,
  publicModelId,
} from "./config.mjs";


const MAX_UINT64 = (1n << 64n) - 1n;


function record(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value;
}


function positiveUint64(value, label) {
  if (typeof value !== "bigint" || value <= 0n || value > MAX_UINT64) {
    throw new TypeError(`${label} must be a positive uint64`);
  }
  return value;
}


function validateMember(model, label) {
  record(model, label);
  publicModelId(model.id, `${label} ID`);
  record(model.capabilities, `${label} capabilities`);
  if (!Array.isArray(model.extensions)) {
    throw new TypeError(`${label} extensions must be an array`);
  }
  if (typeof model.inferenceFormat !== "string" || model.inferenceFormat.length === 0) {
    throw new TypeError(`${label} inference format must be non-empty`);
  }
  positiveUint64(model.contextWindowTokens, `${label} context window`);
  positiveUint64(model.maxOutputTokens, `${label} maximum output`);
  return model;
}


function validatedConfig(config) {
  record(config, "pooler configuration");
  const hasModelMembers = config.memberModelIds !== undefined;
  const hasProviderMembers = config.memberProviders !== undefined;
  if (hasModelMembers === hasProviderMembers) {
    throw new TypeError(
      "pooler configuration requires either member models or member providers",
    );
  }

  if (hasProviderMembers) {
    if (!Array.isArray(config.memberProviders) || config.memberProviders.length < 2) {
      throw new TypeError("pooler configuration requires at least two member providers");
    }
    for (const provider of config.memberProviders) {
      providerPrefix(provider, "configured member provider");
    }
    if (new Set(config.memberProviders).size !== config.memberProviders.length) {
      throw new TypeError("configured member providers must be distinct");
    }
    if (config.outputModel !== undefined) {
      throw new TypeError("provider-wide pooler configuration cannot name one output model");
    }
    return Object.freeze({
      mode: "providers",
      memberProviders: config.memberProviders,
    });
  }

  if (!Array.isArray(config.memberModelIds) || config.memberModelIds.length < 2) {
    throw new TypeError("pooler configuration requires at least two member models");
  }
  for (const id of config.memberModelIds) publicModelId(id, "configured member model ID");
  if (new Set(config.memberModelIds).size !== config.memberModelIds.length) {
    throw new TypeError("configured member model IDs must be distinct");
  }
  if (typeof config.outputModel !== "string") {
    throw new TypeError("pooler configuration has no output model");
  }
  return Object.freeze({
    mode: "models",
    memberModelIds: config.memberModelIds,
    outputModel: config.outputModel,
  });
}


function minimum(models, field) {
  return models.reduce(
    (result, model) => (model[field] < result ? model[field] : result),
    models[0][field],
  );
}


function modelIdParts(id) {
  const separator = id.indexOf("/");
  return Object.freeze({
    provider: id.slice(0, separator),
    localModel: id.slice(separator + 1),
  });
}


function valuesSummary(values, limit = 20) {
  if (values.length === 0) return "(none)";
  const visible = values.slice(0, limit).join(", ");
  return values.length <= limit
    ? visible
    : `${visible} (+${values.length - limit} more)`;
}


function availableProviderSummary(index) {
  return valuesSummary([
    ...new Set([...index.keys()].map((id) => modelIdParts(id).provider)),
  ].sort());
}


function composePool(outputId, members) {
  for (const model of members) validateMember(model, `pooler member ${model.id}`);
  const reference = members[0];
  for (const model of members.slice(1)) {
    if (model.inferenceFormat !== reference.inferenceFormat) {
      throw new TypeError(
        `pooler members have incompatible inference formats for ${outputId}: `
        + members.map((entry) => (
          `${entry.id}=${JSON.stringify(entry.inferenceFormat)}`
        )).join(", "),
      );
    }
    if (!isDeepStrictEqual(model.capabilities, reference.capabilities)) {
      throw new TypeError(
        `pooler members have incompatible capabilities for ${outputId}: `
        + members.map((entry) => entry.id).join(", "),
      );
    }
    if (!isDeepStrictEqual(model.extensions, reference.extensions)) {
      throw new TypeError(
        `pooler members have incompatible extensions for ${outputId}: `
        + members.map((entry) => entry.id).join(", "),
      );
    }
  }

  const virtualModel = Object.freeze({
    id: outputId,
    displayName: outputId,
    capabilities: structuredClone(reference.capabilities),
    contextWindowTokens: minimum(members, "contextWindowTokens"),
    maxOutputTokens: minimum(members, "maxOutputTokens"),
    extensions: structuredClone(reference.extensions),
    inferenceFormat: reference.inferenceFormat,
  });

  return Object.freeze({
    memberModelIds: Object.freeze(members.map((model) => model.id)),
    members: Object.freeze(members),
    virtualModel,
  });
}


function exactModelPools(index, config, componentName) {
  const outputId = outputModelId(componentName, config.outputModel);
  if (index.has(outputId)) {
    throw new TypeError(`pooler output model collides with upstream model ${outputId}`);
  }
  const members = config.memberModelIds.map((id) => {
    const model = index.get(id);
    if (model === undefined) {
      throw new TypeError(
        `pooler member model is unavailable: ${id}; available upstream providers: `
        + availableProviderSummary(index),
      );
    }
    return model;
  });
  return [composePool(outputId, members)];
}


function providerWidePools(index, config, componentName) {
  const selected = new Set(config.memberProviders);
  const modelsByProvider = new Map(
    config.memberProviders.map((provider) => [provider, new Map()]),
  );
  const localModels = [];
  const sawLocalModel = new Set();

  for (const [id, model] of index) {
    const { provider, localModel } = modelIdParts(id);
    if (!selected.has(provider)) continue;
    modelsByProvider.get(provider).set(localModel, model);
    if (!sawLocalModel.has(localModel)) {
      sawLocalModel.add(localModel);
      localModels.push(localModel);
    }
  }

  for (const provider of config.memberProviders) {
    if (modelsByProvider.get(provider).size === 0) {
      throw new TypeError(
        `configured member provider ${JSON.stringify(provider)} is unavailable; `
        + `configured providers: ${config.memberProviders.join(", ")}; `
        + `available upstream providers: ${availableProviderSummary(index)}`,
      );
    }
  }

  const pools = [];
  for (const localModel of localModels) {
    const members = config.memberProviders.flatMap((provider) => {
      const model = modelsByProvider.get(provider).get(localModel);
      return model === undefined ? [] : [model];
    });
    if (members.length < 2) continue;

    const outputId = outputModelId(componentName, localModel);
    if (index.has(outputId)) {
      throw new TypeError(`pooler output model collides with upstream model ${outputId}`);
    }
    pools.push(composePool(outputId, members));
  }

  if (pools.length === 0) {
    const counts = config.memberProviders.map((provider) => (
      `${provider}=${modelsByProvider.get(provider).size}`
    )).join(", ");
    throw new TypeError(
      "pooler has no model available from at least two configured member providers; "
      + `provider model counts: ${counts}; `
      + "provider-local model IDs must match exactly to form a pool",
    );
  }
  return pools;
}


export function composeCatalog(upstreamModels, config, componentName) {
  if (!Array.isArray(upstreamModels)) {
    throw new TypeError("upstream models must be an array");
  }
  const parsed = validatedConfig(config);
  const index = new Map();

  for (let position = 0; position < upstreamModels.length; position += 1) {
    const model = record(upstreamModels[position], `upstream model ${position}`);
    const id = publicModelId(model.id, `upstream model ${position} ID`);
    if (index.has(id)) throw new TypeError(`upstream repeats model ${id}`);
    index.set(id, model);
  }
  const pools = parsed.mode === "models"
    ? exactModelPools(index, parsed, componentName)
    : providerWidePools(index, parsed, componentName);

  return Object.freeze({
    models: Object.freeze([
      ...upstreamModels,
      ...pools.map((pool) => pool.virtualModel),
    ]),
    pools: Object.freeze(pools),
  });
}
