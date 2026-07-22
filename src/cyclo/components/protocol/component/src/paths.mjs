import { isAbsolute } from "node:path";

const REQUIREMENT_NAME = /^[a-z][a-z0-9-]*$/u;

export const DEFAULT_COMPONENT_SOCKET = "/run/cyclo/component.sock";

export function componentSocketPath(env = process.env) {
  return configuredPath(env.CYCLO_COMPONENT_SOCKET) ?? DEFAULT_COMPONENT_SOCKET;
}

export function requirementSocketPath(name, env = process.env) {
  checkRequirementName(name);
  return configuredPath(env[requirementVariable(name, "SOCKET")])
    ?? `/run/cyclo/requirements/${name}/component.sock`;
}

function requirementVariable(name, suffix) {
  return `CYCLO_REQUIRE_${name.replaceAll("-", "_").toUpperCase()}_${suffix}`;
}

function configuredPath(value) {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError("component binding paths must be non-empty strings");
  }
  const path = value.trim();
  if (!isAbsolute(path)) {
    throw new TypeError("component binding paths must be absolute");
  }
  return path;
}

function checkRequirementName(name) {
  if (typeof name !== "string" || !REQUIREMENT_NAME.test(name)) {
    throw new TypeError("requirement name is invalid");
  }
}
