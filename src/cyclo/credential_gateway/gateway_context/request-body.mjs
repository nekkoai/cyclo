import { zstdDecompress } from "node:zlib";
import { promisify } from "node:util";


const decompressZstd = promisify(zstdDecompress);

const CONTENT_CODING_TOKEN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/u;
const SUPPORTED_CONTENT_ENCODINGS = new Set(["identity", "zstd"]);

// Request headers forwarded upstream. The caller has already authenticated the
// request and validated Content-Encoding before this allow-list is applied.
// Client credentials are intentionally absent and replaced by the gateway.
export const FORWARDED_REQUEST_HEADERS = new Set([
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
  // github-copilot editor identity + per-request hints (pi sets these)
  "copilot-integration-id",
  "editor-version",
  "editor-plugin-version",
  "x-initiator",
  "openai-intent",
  "copilot-vision-request",
]);

class RequestBodyError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = "RequestBodyError";
    this.statusCode = statusCode;
  }
}

function contentEncoding(value) {
  if (value === undefined) return "identity";
  if (typeof value !== "string") {
    throw new RequestBodyError(400, "malformed content-encoding");
  }

  const codings = value.split(",").map((coding) => coding.trim());
  if (
    codings.length === 0 ||
    codings.some((coding) => !coding || !CONTENT_CODING_TOKEN.test(coding))
  ) {
    throw new RequestBodyError(400, "malformed content-encoding");
  }
  if (codings.length !== 1) {
    throw new RequestBodyError(415, "unsupported content-encoding");
  }

  const normalized = codings[0].toLowerCase();
  if (!SUPPORTED_CONTENT_ENCODINGS.has(normalized)) {
    throw new RequestBodyError(415, "unsupported content-encoding");
  }
  return normalized;
}

// Return separate policy and upstream views of a request body. Policy checks
// inspect bounded, decoded bytes; forwarding always retains the exact original
// bytes so Content-Encoding continues to describe the upstream payload.
export async function prepareRequestBody(body, encodingHeader, maxDecodedBytes) {
  if (!Buffer.isBuffer(body)) {
    throw new TypeError("request body must be a Buffer");
  }
  if (!Number.isSafeInteger(maxDecodedBytes) || maxDecodedBytes <= 0) {
    throw new RangeError("max decoded request body must be a positive safe integer");
  }

  const encoding = contentEncoding(encodingHeader);
  if (encoding === "identity") {
    return { policyBody: body, upstreamBody: body };
  }

  try {
    const policyBody = await decompressZstd(body, {
      maxOutputLength: maxDecodedBytes,
    });
    return { policyBody, upstreamBody: body };
  } catch (exc) {
    if (exc?.code === "ERR_BUFFER_TOO_LARGE") {
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
