import abi from "./abi.json" with { type: "json" };

export const MAX_PROVIDER_PREFIX_LENGTH = 64;
export const MAX_LOCAL_MODEL_ID_UTF16_UNITS = 1_024;
const RESERVED_PROVIDER_PREFIXES = new Set([
  "__proto__",
  "constructor",
  "gateway",
  "prototype",
]);

const PROVIDER_PREFIX = /^[a-z0-9][a-z0-9_-]*$/u;
const LOCAL_MODEL_ID = /^[^\p{White_Space}\ufeff\u0000-\u001f\u007f\ud800-\udfff]+$/u;

// Cyclo transports the JSON representation used by its pinned Pi runtime.
// This is an ABI identifier, not a second inference schema.
export const PI_INFERENCE_FORMAT = abi.piInferenceFormat;

export function isProviderPrefix(value) {
  return typeof value === "string"
    && value.length <= MAX_PROVIDER_PREFIX_LENGTH
    && PROVIDER_PREFIX.test(value)
    && !RESERVED_PROVIDER_PREFIXES.has(value);
}

export function isLocalModelId(value) {
  return typeof value === "string"
    && value.length <= MAX_LOCAL_MODEL_ID_UTF16_UNITS
    && LOCAL_MODEL_ID.test(value);
}

export function splitPublicModelId(value) {
  if (typeof value !== "string") return undefined;
  const separator = value.indexOf("/");
  if (separator < 0) return undefined;
  const provider = value.slice(0, separator);
  const model = value.slice(separator + 1);
  return isProviderPrefix(provider) && isLocalModelId(model)
    ? { provider, model }
    : undefined;
}

export function encodePayload(value) {
  const payload = JSON.stringify(value);
  if (payload === undefined) {
    throw new TypeError("Pi payload is not JSON-serializable");
  }
  return payload;
}

export function decodePayload(payload) {
  if (typeof payload !== "string") throw new TypeError("Pi payload is not a string");
  return JSON.parse(payload);
}
