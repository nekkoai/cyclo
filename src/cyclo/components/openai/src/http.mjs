import { timingSafeEqual } from "node:crypto";
import { once } from "node:events";
import { createServer } from "node:http";

import { Code, ConnectError } from "@connectrpc/connect";
import { resourceExhaustedRetryAt } from "@cyclo/provider/errors";

import {
  OpenAIRequestError,
  OpenAIUpstreamError,
  getOpenAIModel,
  listOpenAIModels,
  openAIModel,
  streamOpenAIResponse,
} from "./adapter.mjs";

export const MAX_OPENAI_REQUEST_BYTES = 8 * 1024 * 1024;
const DEFAULT_CATALOGUE_TIMEOUT_MS = 10_000;

export function createOpenAIHTTPServer({
  client,
  apiKey,
  shutdownSignal,
  maxRequestBytes = MAX_OPENAI_REQUEST_BYTES,
  catalogueTimeoutMs = DEFAULT_CATALOGUE_TIMEOUT_MS,
  now = Date.now,
  idFactory,
  onInvalid,
} = {}) {
  if (!client || typeof client.listModels !== "function" || typeof client.infer !== "function") {
    throw new TypeError("a Cyclo Provider client is required");
  }
  if (apiKey !== undefined && (typeof apiKey !== "string" || !apiKey)) {
    throw new TypeError("apiKey must be a non-empty string");
  }
  if (!Number.isSafeInteger(maxRequestBytes) || maxRequestBytes <= 0) {
    throw new TypeError("maxRequestBytes must be a positive integer");
  }
  if (!Number.isSafeInteger(catalogueTimeoutMs) || catalogueTimeoutMs <= 0) {
    throw new TypeError("catalogueTimeoutMs must be a positive integer");
  }

  const server = createServer((request, response) => {
    void serve(request, response, {
      client,
      apiKey,
      shutdownSignal,
      maxRequestBytes,
      catalogueTimeoutMs,
      now,
      idFactory,
      onInvalid,
    }).catch((error) => {
      if (response.headersSent) {
        response.destroy(error instanceof Error ? error : undefined);
        return;
      }
      sendError(response, error, { now });
    });
  });

  if (shutdownSignal) {
    if (shutdownSignal.aborted) server.close();
    else shutdownSignal.addEventListener("abort", () => server.close(), { once: true });
  }
  return server;
}

async function serve(request, response, options) {
  applyCommonHeaders(response);
  if (!authorized(request, options.apiKey)) {
    response.setHeader("www-authenticate", 'Bearer realm="cyclo-openai"');
    throw new OpenAIRequestError("Incorrect API key provided", {
      status: 401,
      type: "authentication_error",
      code: "invalid_api_key",
    });
  }

  const url = requestURL(request);
  if (request.method === "GET" && url.pathname === "/v1/models") {
    const signal = requestSignal(request, response, options.shutdownSignal);
    const result = await listOpenAIModels(options.client, {
      signal,
      timeoutMs: options.catalogueTimeoutMs,
      onInvalid: options.onInvalid,
    });
    sendJSON(response, 200, result);
    return;
  }

  if (request.method === "GET" && url.pathname.startsWith("/v1/models/")) {
    const raw = url.pathname.slice("/v1/models/".length);
    let id;
    try {
      id = decodeURIComponent(raw);
    } catch {
      throw new OpenAIRequestError("model path is not valid URL encoding", {
        status: 404,
        code: "model_not_found",
        param: "model",
      });
    }
    const signal = requestSignal(request, response, options.shutdownSignal);
    const model = await getOpenAIModel(options.client, id, {
      signal,
      timeoutMs: options.catalogueTimeoutMs,
      onInvalid: options.onInvalid,
    });
    sendJSON(response, 200, openAIModel(model));
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/responses") {
    const signal = requestSignal(request, response, options.shutdownSignal);
    const document = await readJSON(request, options.maxRequestBytes, signal);
    await serveResponse(response, document, {
      ...options,
      signal,
    });
    return;
  }

  if (["/v1/models", "/v1/responses"].includes(url.pathname)) {
    response.setHeader("allow", url.pathname === "/v1/models" ? "GET" : "POST");
    throw new OpenAIRequestError("Method not allowed", {
      status: 405,
      code: "method_not_allowed",
    });
  }
  throw new OpenAIRequestError(`Unknown endpoint: ${safePath(url.pathname)}`, {
    status: 404,
    code: "not_found",
  });
}

async function serveResponse(response, document, options) {
  const stream = streamOpenAIResponse(options.client, document, {
    signal: options.signal,
    now: options.now,
    ...(options.idFactory ? { idFactory: options.idFactory } : {}),
    onInvalid: options.onInvalid,
  });
  const iterator = stream[Symbol.asyncIterator]();
  let first;
  try {
    first = await iterator.next();
  } catch (error) {
    throw error;
  }
  if (first.done) throw new OpenAIUpstreamError();

  if (document?.stream === true) {
    response.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    });
    await writeSSE(response, first.value, options.signal);
    for await (const event of { [Symbol.asyncIterator]: () => iterator }) {
      await writeSSE(response, event, options.signal);
    }
    if (!response.destroyed) response.end();
    return;
  }

  let terminal = terminalResponse(first.value);
  for await (const event of { [Symbol.asyncIterator]: () => iterator }) {
    terminal = terminalResponse(event) ?? terminal;
  }
  if (!terminal) throw new OpenAIUpstreamError();
  sendJSON(response, 200, terminal);
}

function terminalResponse(event) {
  return ["response.completed", "response.incomplete", "response.failed"].includes(event?.type)
    ? event.response
    : undefined;
}

async function writeSSE(response, event, signal) {
  if (signal.aborted || response.destroyed) return;
  const frame = `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
  if (!response.write(frame)) {
    await once(response, "drain", { signal });
  }
}

async function readJSON(request, limit, signal) {
  const contentType = request.headers["content-type"];
  const type = typeof contentType === "string"
    ? contentType.split(";", 1)[0].trim().toLowerCase()
    : undefined;
  if (type !== "application/json") {
    throw new OpenAIRequestError("Content-Type must be application/json", {
      status: 415,
      code: "unsupported_media_type",
    });
  }
  const encoding = request.headers["content-encoding"];
  if (encoding !== undefined
      && (typeof encoding !== "string" || encoding.toLowerCase() !== "identity")) {
    throw new OpenAIRequestError("Content-Encoding is not supported", {
      status: 415,
      code: "unsupported_content_encoding",
    });
  }
  const declared = request.headers["content-length"];
  if (declared !== undefined) {
    const length = Number(declared);
    if (!Number.isSafeInteger(length) || length < 0) {
      throw new OpenAIRequestError("Content-Length is invalid", {
        status: 400,
        code: "invalid_request_error",
      });
    }
    if (length > limit) throw requestTooLarge();
  }

  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    signal.throwIfAborted();
    length += chunk.length;
    if (length > limit) throw requestTooLarge();
    chunks.push(chunk);
  }
  let document;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
    document = JSON.parse(text);
  } catch {
    throw new OpenAIRequestError("Request body is not valid JSON", {
      status: 400,
      code: "invalid_json",
    });
  }
  return document;
}

function requestTooLarge() {
  return new OpenAIRequestError("Request body is too large", {
    status: 413,
    code: "request_too_large",
  });
}

function requestSignal(request, response, shutdownSignal) {
  const disconnected = new AbortController();
  const abort = () => {
    if (!disconnected.signal.aborted && !response.writableEnded) {
      disconnected.abort(new ConnectError("OpenAI client disconnected", Code.Canceled));
    }
  };
  request.once("aborted", abort);
  response.once("close", abort);
  const signals = [disconnected.signal];
  if (shutdownSignal) signals.push(shutdownSignal);
  return AbortSignal.any(signals);
}

function authorized(request, expected) {
  if (expected === undefined) return true;
  const value = request.headers.authorization;
  if (typeof value !== "string" || !value.startsWith("Bearer ")) return false;
  const supplied = Buffer.from(value.slice("Bearer ".length));
  const wanted = Buffer.from(expected);
  return supplied.length === wanted.length && timingSafeEqual(supplied, wanted);
}

export function openAIError(error, { now = Date.now } = {}) {
  if (error instanceof OpenAIRequestError) {
    return {
      status: error.status,
      headers: {},
      body: errorDocument(error.message, error.type, error.param, error.code),
    };
  }
  if (error instanceof ConnectError) {
    const retryAt = resourceExhaustedRetryAt(error);
    if (retryAt) {
      const seconds = Math.max(1, Math.ceil((retryAt.getTime() - now()) / 1_000));
      return {
        status: 429,
        headers: { "retry-after": String(seconds) },
        body: errorDocument(
          "Cyclo Provider capacity is exhausted; retry later",
          "rate_limit_error",
          null,
          "rate_limit_exceeded",
        ),
      };
    }
    const mapped = connectStatus(error.code);
    return {
      status: mapped.status,
      headers: {},
      body: errorDocument(mapped.message, mapped.type, null, mapped.code),
    };
  }
  if (error instanceof OpenAIUpstreamError) {
    return {
      status: 502,
      headers: {},
      body: errorDocument(
        "Cyclo Provider returned an invalid response",
        "server_error",
        null,
        "upstream_protocol_error",
      ),
    };
  }
  return {
    status: 500,
    headers: {},
    body: errorDocument(
      "The Cyclo OpenAI bridge encountered an internal error",
      "server_error",
      null,
      "internal_error",
    ),
  };
}

function connectStatus(code) {
  if (code === Code.InvalidArgument) {
    return {
      status: 400,
      type: "invalid_request_error",
      code: "invalid_request_error",
      message: "Cyclo Provider rejected the request",
    };
  }
  if (code === Code.NotFound) {
    return {
      status: 404,
      type: "invalid_request_error",
      code: "model_not_found",
      message: "The requested Cyclo Provider model was not found",
    };
  }
  if (code === Code.Unauthenticated) {
    return {
      status: 401,
      type: "authentication_error",
      code: "provider_unauthenticated",
      message: "Cyclo Provider authentication failed",
    };
  }
  if (code === Code.PermissionDenied) {
    return {
      status: 403,
      type: "permission_error",
      code: "provider_permission_denied",
      message: "Cyclo Provider denied the request",
    };
  }
  if (code === Code.ResourceExhausted) {
    return {
      status: 429,
      type: "rate_limit_error",
      code: "rate_limit_exceeded",
      message: "Cyclo Provider capacity is exhausted",
    };
  }
  if (code === Code.DeadlineExceeded) {
    return {
      status: 504,
      type: "server_error",
      code: "provider_timeout",
      message: "Cyclo Provider timed out",
    };
  }
  if (code === Code.Canceled) {
    return {
      status: 499,
      type: "server_error",
      code: "request_canceled",
      message: "The request was canceled",
    };
  }
  if (code === Code.Unavailable) {
    return {
      status: 503,
      type: "server_error",
      code: "provider_unavailable",
      message: "Cyclo Provider is unavailable",
    };
  }
  return {
    status: 502,
    type: "server_error",
    code: "provider_error",
    message: "Cyclo Provider request failed",
  };
}

function sendError(response, error, options) {
  if (response.destroyed) return;
  const converted = openAIError(error, options);
  for (const [name, value] of Object.entries(converted.headers)) {
    response.setHeader(name, value);
  }
  sendJSON(response, converted.status, converted.body);
}

function errorDocument(message, type, param, code) {
  return { error: { message, type, param, code } };
}

function sendJSON(response, status, document) {
  const body = Buffer.from(JSON.stringify(document));
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": String(body.length),
  });
  response.end(body);
}

function applyCommonHeaders(response) {
  response.setHeader("x-content-type-options", "nosniff");
  response.setHeader("referrer-policy", "no-referrer");
  response.setHeader("cache-control", "no-store");
}

function requestURL(request) {
  try {
    return new URL(request.url ?? "/", "http://cyclo.invalid");
  } catch {
    throw new OpenAIRequestError("Request URL is invalid", {
      status: 400,
      code: "invalid_request_error",
    });
  }
}

function safePath(value) {
  return String(value)
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, 256) || "/";
}
