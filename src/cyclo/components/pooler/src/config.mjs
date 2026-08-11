import {
  isLocalModelId,
  isProviderPrefix,
  splitPublicModelId,
} from "@cyclo/provider/protocol";

const DCOMP_COMPONENT_NAME = /^[a-z][a-z0-9-]{0,62}$/u;
const PARAMETER = /^([a-z][a-z0-9_-]*)=/u;


function localModelId(value, label) {
  if (!isLocalModelId(value)) {
    throw new TypeError(`invalid ${label}: ${JSON.stringify(value)}`);
  }
  return value;
}


export function providerPrefix(value, label = "provider prefix") {
  if (!isProviderPrefix(value)) {
    throw new TypeError(`invalid ${label}: ${JSON.stringify(value)}`);
  }
  return value;
}


export function publicModelId(value, label = "public model ID") {
  if (typeof value !== "string") {
    throw new TypeError(`invalid ${label}: ${JSON.stringify(value)}`);
  }
  if (splitPublicModelId(value) !== undefined) return value;
  const separator = value.indexOf("/");
  if (separator < 0) {
    throw new TypeError(`${label} must be PROVIDER/MODEL`);
  }
  providerPrefix(value.slice(0, separator), `${label} provider`);
  localModelId(value.slice(separator + 1), `${label} local model`);
  return value;
}


export function parseComponentName(value) {
  if (typeof value !== "string" || !DCOMP_COMPONENT_NAME.test(value)) {
    throw new TypeError(`invalid DCOMP_COMPONENT_NAME: ${JSON.stringify(value)}`);
  }
  return value;
}


export function outputModelId(componentName, outputModel) {
  const prefix = parseComponentName(componentName);
  return publicModelId(
    `${prefix}/${localModelId(outputModel, "output model")}`,
    "output public model ID",
  );
}


export function parseArguments(argv) {
  if (!Array.isArray(argv)) {
    throw new TypeError("pooler arguments must be an array");
  }

  const members = [];
  let outputModel;
  let sawParameter = false;

  for (const argument of argv) {
    if (typeof argument !== "string") {
      throw new TypeError("pooler arguments must be strings");
    }

    const parameter = PARAMETER.exec(argument);
    if (parameter !== null) {
      sawParameter = true;
      const key = parameter[1];
      if (key !== "model") {
        throw new TypeError(`unknown pooler parameter: ${JSON.stringify(key)}`);
      }
      if (outputModel !== undefined) {
        throw new TypeError("duplicate pooler parameter: model");
      }
      outputModel = localModelId(argument.slice(parameter[0].length), "output model");
      continue;
    }

    if (sawParameter) {
      throw new TypeError("all pool members must precede pooler parameters");
    }
    members.push(argument);
  }

  if (members.length < 2) {
    throw new TypeError("pooler requires at least two members");
  }

  const modelMemberCount = members.filter((member) => member.includes("/")).length;
  if (modelMemberCount === 0) {
    const memberProviders = members.map((provider) => (
      providerPrefix(provider, "member provider")
    ));
    if (new Set(memberProviders).size !== memberProviders.length) {
      throw new TypeError("pooler member providers must be distinct");
    }
    if (outputModel !== undefined) {
      throw new TypeError("model=<output-model> is only valid with member model IDs");
    }
    return Object.freeze({
      memberProviders: Object.freeze(memberProviders),
    });
  }

  if (modelMemberCount !== members.length) {
    throw new TypeError("pooler cannot mix member providers and member model IDs");
  }
  const memberModelIds = members.map((id) => publicModelId(id, "member model ID"));
  if (new Set(memberModelIds).size !== memberModelIds.length) {
    throw new TypeError("pooler member model IDs must be distinct");
  }
  if (outputModel === undefined) {
    throw new TypeError("pooler requires model=<output-model>");
  }

  return Object.freeze({
    memberModelIds: Object.freeze(memberModelIds),
    outputModel,
  });
}
