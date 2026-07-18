import { zstdDecompress } from "node:zlib";
import { promisify } from "node:util";


const decompressZstd = promisify(zstdDecompress);
const SIMPLE_INFERENCE_PATHS = new Set([
  "/chat/completions",
  "/v1/chat/completions",
  "/responses",
  "/v1/responses",
  "/codex/responses",
  "/v1/codex/responses",
  "/messages",
  "/v1/messages",
]);
const GOOGLE_INFERENCE_ACTIONS = ["generateContent", "streamGenerateContent"];
const GOOGLE_INTERNAL_PATHS = new Set(
  GOOGLE_INFERENCE_ACTIONS.flatMap((action) => [
    `/v1internal:${action}`,
    `/v1internal/${action}`,
  ]),
);
const GOOGLE_MODEL_PATH = new RegExp(
  `^/(?:v1/|v1beta/)?models/([^/]+):(${GOOGLE_INFERENCE_ACTIONS.join("|")})$`,
  "u",
);
const GOOGLE_VERTEX_MODEL_PATH = new RegExp(
  `^/v1/projects/[^/]+/locations/[^/]+/publishers/google/models/([^/]+):(${GOOGLE_INFERENCE_ACTIONS.join("|")})$`,
  "u",
);
const CONTENT_CODING_TOKEN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/u;
const SUPPORTED_CONTENT_ENCODINGS = new Set(["identity", "zstd"]);
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/u;
const MARKER_BYTES = Buffer.from("[REDACTED]", "ascii");
const EMPTY = Buffer.alloc(0);

const FORWARDED_REQUEST_HEADERS = new Set([
  "accept",
  "content-type",
  "content-encoding",
  "anthropic-version",
  "anthropic-beta",
  "anthropic-dangerous-direct-browser-access",
  "x-app",
  "openai-beta",
  "session_id",
  "x-client-request-id",
  "user-agent",
  "copilot-integration-id",
  "editor-version",
  "editor-plugin-version",
  "x-initiator",
  "openai-intent",
  "copilot-vision-request",
]);


function normalizedPath(path) {
  if (typeof path !== "string" || !path.startsWith("/")) return null;
  return path.length > 1 ? path.replace(/\/+$/u, "") : path;
}


export function modelFromGooglePath(path) {
  const normalized = normalizedPath(path);
  if (!normalized) return null;
  const match = normalized.match(GOOGLE_MODEL_PATH)
    ?? normalized.match(GOOGLE_VERTEX_MODEL_PATH);
  if (!match) return null;
  try {
    const model = decodeURIComponent(match[1]);
    return model
      && !/[\\\s\u0000-\u001f\u007f]/u.test(model)
      && !model.split("/").some((segment) => segment === "." || segment === "..")
      ? model
      : null;
  } catch {
    return null;
  }
}


export function isKnownInferenceEndpoint(method, path) {
  if (method !== "POST") return false;
  const normalized = normalizedPath(path);
  return normalized !== null && (
    SIMPLE_INFERENCE_PATHS.has(normalized)
    || GOOGLE_INTERNAL_PATHS.has(normalized)
    || modelFromGooglePath(normalized) !== null
  );
}


function topLevelObjectKeys(text) {
  const keys = [];
  let offset = 0;
  const whitespace = /\s/u;
  const skipWhitespace = () => {
    while (offset < text.length && whitespace.test(text[offset])) offset += 1;
  };
  skipWhitespace();
  if (text[offset] !== "{") return null;
  offset += 1;
  skipWhitespace();
  if (text[offset] === "}") return keys;
  while (offset < text.length) {
    skipWhitespace();
    if (text[offset] !== '"') return null;
    const start = offset;
    offset += 1;
    let escaped = false;
    while (offset < text.length) {
      const character = text[offset];
      offset += 1;
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') break;
    }
    keys.push(JSON.parse(text.slice(start, offset)));
    skipWhitespace();
    if (text[offset] !== ":") return null;
    offset += 1;
    let depth = 0;
    let inString = false;
    escaped = false;
    for (; offset < text.length; offset += 1) {
      const character = text[offset];
      if (inString) {
        if (escaped) escaped = false;
        else if (character === "\\") escaped = true;
        else if (character === '"') inString = false;
        continue;
      }
      if (character === '"') inString = true;
      else if (character === "{" || character === "[") depth += 1;
      else if (character === "}" || character === "]") {
        if (depth > 0) depth -= 1;
        else break;
      } else if (character === "," && depth === 0) break;
    }
    if (text[offset] === ",") {
      offset += 1;
      continue;
    }
    if (text[offset] === "}") return keys;
    return null;
  }
  return null;
}


export function modelFromInferenceRequest(path, body) {
  const pathModel = modelFromGooglePath(path);
  if (pathModel) return pathModel;
  if (!body || body.length === 0) return null;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.from(body));
    const parsed = JSON.parse(text);
    const keys = topLevelObjectKeys(text);
    if (
      !parsed
      || typeof parsed !== "object"
      || Array.isArray(parsed)
      || !keys
      || keys.filter((key) => key === "model").length !== 1
    ) {
      return null;
    }
    return typeof parsed.model === "string" && parsed.model ? parsed.model : null;
  } catch {
    return null;
  }
}


class RequestBodyError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}


function contentEncoding(value) {
  if (value === undefined) return "identity";
  if (typeof value !== "string") throw new RequestBodyError(400, "malformed content-encoding");
  const codings = value.split(",").map((coding) => coding.trim());
  if (!codings.length || codings.some((coding) => !coding || !CONTENT_CODING_TOKEN.test(coding))) {
    throw new RequestBodyError(400, "malformed content-encoding");
  }
  if (codings.length !== 1) throw new RequestBodyError(415, "unsupported content-encoding");
  const normalized = codings[0].toLowerCase();
  if (!SUPPORTED_CONTENT_ENCODINGS.has(normalized)) {
    throw new RequestBodyError(415, "unsupported content-encoding");
  }
  return normalized;
}


export async function prepareRequestBody(body, encodingHeader, maxDecodedBytes) {
  if (!Buffer.isBuffer(body)) throw new TypeError("request body must be a Buffer");
  const encoding = contentEncoding(encodingHeader);
  if (encoding === "identity") return { policyBody: body, upstreamBody: body };
  try {
    const policyBody = await decompressZstd(body, { maxOutputLength: maxDecodedBytes });
    return { policyBody, upstreamBody: body };
  } catch (error) {
    if (error?.code === "ERR_BUFFER_TOO_LARGE") {
      throw new RequestBodyError(413, "decoded request body too large");
    }
    throw new RequestBodyError(400, "malformed zstd request body");
  }
}


export function forwardedRequestHeaders(requestHeaders) {
  const headers = { "accept-encoding": "identity" };
  for (const [key, value] of Object.entries(requestHeaders)) {
    if (FORWARDED_REQUEST_HEADERS.has(key.toLowerCase()) && typeof value === "string") {
      headers[key] = value;
    }
  }
  return headers;
}


function copyBytes(value, label, allowString = true) {
  if (allowString && typeof value === "string") return Buffer.from(value, "utf8");
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) return Buffer.from(value);
  throw new TypeError(`${label} must be a string, Buffer, or Uint8Array`);
}


export function normalizeResponseSecrets(secrets) {
  if (
    secrets === null
    || secrets === undefined
    || typeof secrets === "string"
    || Buffer.isBuffer(secrets)
    || secrets instanceof Uint8Array
    || typeof secrets[Symbol.iterator] !== "function"
  ) {
    throw new TypeError("response secrets must be an iterable of byte values");
  }
  const unique = new Map();
  for (const value of secrets) {
    const secret = copyBytes(value, "response secret");
    if (!secret.length) throw new TypeError("response secrets must not be empty");
    if (MARKER_BYTES.indexOf(secret) !== -1) {
      throw new TypeError("a response secret conflicts with the redaction marker");
    }
    unique.set(secret.toString("hex"), secret);
  }
  return Object.freeze(
    [...unique.values()].sort(
      (left, right) => right.length - left.length || Buffer.compare(left, right),
    ),
  );
}


export function createResponseSecretRedactor(secrets) {
  const normalized = normalizeResponseSecrets(secrets);
  const byFirstByte = new Map();
  let maxLength = 0;
  for (const secret of normalized) {
    maxLength = Math.max(maxLength, secret.length);
    const patterns = byFirstByte.get(secret[0]) ?? [];
    patterns.push(secret);
    byFirstByte.set(secret[0], patterns);
  }
  let pending = EMPTY;
  let open = true;
  const process = (bytes, final) => {
    if (!maxLength) return bytes;
    const limit = final ? bytes.length : Math.max(0, bytes.length - maxLength + 1);
    const parts = [];
    let outputLength = 0;
    let scan = 0;
    let plainStart = 0;
    while (scan < limit) {
      const patterns = byFirstByte.get(bytes[scan]) ?? [];
      const secret = patterns.find(
        (item) => scan + item.length <= bytes.length
          && bytes.subarray(scan, scan + item.length).equals(item),
      );
      if (!secret) {
        scan += 1;
        continue;
      }
      if (plainStart < scan) {
        const plain = bytes.subarray(plainStart, scan);
        parts.push(plain);
        outputLength += plain.length;
      }
      parts.push(MARKER_BYTES);
      outputLength += MARKER_BYTES.length;
      scan += secret.length;
      plainStart = scan;
    }
    if (plainStart < scan) {
      const plain = bytes.subarray(plainStart, scan);
      parts.push(plain);
      outputLength += plain.length;
    }
    pending = final ? EMPTY : Buffer.from(bytes.subarray(scan));
    return outputLength ? Buffer.concat(parts, outputLength) : EMPTY;
  };
  return Object.freeze({
    push(chunk) {
      if (!open) throw new Error("response redactor is closed");
      const incoming = copyBytes(chunk, "response chunk", false);
      const bytes = pending.length ? Buffer.concat([pending, incoming]) : incoming;
      return process(bytes, false);
    },
    flush() {
      if (!open) throw new Error("response redactor is closed");
      const result = process(pending, true);
      pending = EMPTY;
      open = false;
      return result;
    },
  });
}


function valueContainsSecret(value, secrets) {
  const values = Array.isArray(value) ? value : [value];
  if (!values.length) return true;
  return values.some((item) => {
    try {
      const bytes = copyBytes(item, "response header value");
      return secrets.some((secret) => bytes.indexOf(secret) !== -1);
    } catch {
      return true;
    }
  });
}


export function filterResponseHeaders(headers, secrets) {
  const normalized = normalizeResponseSecrets(secrets);
  const filtered = Object.create(null);
  const blocked = new Set();
  const entries = typeof headers?.entries === "function"
    ? headers.entries()
    : Object.entries(headers ?? {});
  for (const [rawName, value] of entries) {
    if (typeof rawName !== "string" || !HEADER_NAME.test(rawName)) continue;
    const name = rawName.toLowerCase();
    if (blocked.has(name)) continue;
    if (valueContainsSecret(value, normalized)) {
      blocked.add(name);
      delete filtered[name];
    } else {
      filtered[name] = Array.isArray(value) ? [...value] : value;
    }
  }
  return filtered;
}
