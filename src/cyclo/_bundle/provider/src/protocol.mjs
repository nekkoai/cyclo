// Cyclo transports the JSON representation used by its pinned Pi runtime.
// This is an ABI identifier, not a second inference schema.
export const PI_INFERENCE_FORMAT = "pi-ai@0.81.1";

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
