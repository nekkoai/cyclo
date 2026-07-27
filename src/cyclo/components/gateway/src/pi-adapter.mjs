import { Code, ConnectError } from "@connectrpc/connect";
import { streamSimple as streamAnthropic } from "@earendil-works/pi-ai/api/anthropic-messages";
import { streamSimple as streamCodex } from "@earendil-works/pi-ai/api/openai-codex-responses";
import { streamSimple as streamOpenAI } from "@earendil-works/pi-ai/api/openai-responses";
import { decodePayload, encodePayload } from "@cyclo/provider/protocol";

const DEFAULT_STREAMERS = Object.freeze({
  "anthropic-messages": streamAnthropic,
  "openai-codex-responses": streamCodex,
  "openai-responses": streamOpenAI,
});

class GatewayResponseError extends Error {}

// The gateway terminates the opaque transport because this is where a Pi call
// becomes a native provider request. It understands only the Pi call frame; it
// does not interpret messages, tools, schemas, arguments, or emitted events.
// It does enforce its own credential boundary on serialized native events.
export function createPiAdapter({ streamers = DEFAULT_STREAMERS } = {}) {
  return Object.freeze({
    infer(route, payload, credential, signal) {
      return dispatch(route, payload, credential, signal, streamers);
    },
  });
}

async function* dispatch(route, payload, credential, signal, streamers) {
  const stream = streamers[route.rawModel.api];
  if (typeof stream !== "function") {
    throw new ConnectError("model adapter is unavailable", Code.Unavailable);
  }

  const frame = piCallFrame(payload);
  const localAbort = new AbortController();
  const dispatchSignal = signal
    ? AbortSignal.any([signal, localAbort.signal])
    : localAbort.signal;
  const options = gatewayOptions(
    frame.options,
    credential.apiKey,
    dispatchSignal,
    route.rawModel.api,
  );

  let native;
  try {
    native = stream(route.rawModel, frame.context, options);
  } catch (error) {
    localAbort.abort(new Error("gateway native dispatch stopped"));
    throw upstreamFailure(error, signal);
  }

  try {
    for await (const event of native) {
      let encoded;
      try {
        encoded = encodePayload(event);
      } catch {
        throw new GatewayResponseError(
          "upstream emitted a non-JSON Pi event",
        );
      }
      assertNoCredentialReflection(encoded, credential);
      yield {
        payload: encoded,
        usage: eventUsage(event),
      };
    }
  } catch (error) {
    if (error instanceof GatewayResponseError) {
      throw new ConnectError(error.message, Code.DataLoss);
    }
    throw upstreamFailure(error, signal);
  } finally {
    localAbort.abort(new Error("gateway native dispatch stopped"));
  }
}

function piCallFrame(payload) {
  let frame;
  try {
    frame = decodePayload(payload);
  } catch {
    throw new ConnectError("Pi payload is not valid JSON", Code.InvalidArgument);
  }
  if (!plainObject(frame) || !("context" in frame)) {
    throw new ConnectError("Pi payload has no call frame", Code.InvalidArgument);
  }
  if (frame.options !== undefined && !plainObject(frame.options)) {
    throw new ConnectError("Pi call options are not an object", Code.InvalidArgument);
  }
  return { context: frame.context, options: frame.options ?? {} };
}

// Credentials, arbitrary headers/environment, callbacks, client objects, and
// native transport/retry/timeout controls belong to the gateway process. They
// cannot be selected through the data plane. Every other JSON option is
// forwarded without a Cyclo allowlist.
function gatewayOptions(options, apiKey, signal, api) {
  const {
    apiKey: _apiKey,
    signal: _signal,
    headers: _headers,
    env: _env,
    client: _client,
    onPayload: _onPayload,
    onResponse: _onResponse,
    transport: _transport,
    timeoutMs: _timeoutMs,
    websocketConnectTimeoutMs: _websocketConnectTimeoutMs,
    maxRetries: _maxRetries,
    maxRetryDelayMs: _maxRetryDelayMs,
    ...inference
  } = options;
  return {
    ...inference,
    apiKey,
    signal,
    maxRetries: 0,
    ...(api === "openai-codex-responses" ? { transport: "sse" } : {}),
  };
}

function eventUsage(event) {
  const usage = event?.type === "done"
    ? event.message?.usage
    : event?.type === "error"
      ? event.error?.usage
      : undefined;
  if (!plainObject(usage)) return undefined;
  return {
    inputTokens: safeTokens(usage.input) + safeTokens(usage.cacheRead) + safeTokens(usage.cacheWrite),
    outputTokens: safeTokens(usage.output),
    cachedInputTokens: safeTokens(usage.cacheRead),
    reasoningTokens: safeTokens(usage.reasoning),
  };
}

function safeTokens(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function assertNoCredentialReflection(payload, credential) {
  const secrets = [...new Set([
    credential.apiKey,
    ...(Array.isArray(credential.secretValues) ? credential.secretValues : []),
  ].filter((value) => typeof value === "string" && value))];
  const candidates = secrets.filter((secret) => (
    payload.includes(JSON.stringify(secret).slice(1, -1))
  ));
  if (candidates.length === 0) return;

  if (containsCredential(JSON.parse(payload), candidates)) {
    throw new GatewayResponseError(
      "upstream response contained gateway authentication material",
    );
  }
}

function containsCredential(document, secrets) {
  const pending = [document];
  while (pending.length > 0) {
    const value = pending.pop();
    if (typeof value === "string") {
      if (secrets.some((secret) => value.includes(secret))) return true;
      continue;
    }
    if (value === null || typeof value !== "object") continue;
    for (const [key, child] of Object.entries(value)) {
      if (secrets.some((secret) => key.includes(secret))) return true;
      pending.push(child);
    }
  }
  return false;
}

function upstreamFailure(_error, signal) {
  if (signal?.aborted) {
    if (signal.reason instanceof ConnectError) return signal.reason;
    return new ConnectError("request canceled", Code.Canceled);
  }
  return new ConnectError("upstream inference failed", Code.Unavailable);
}

function plainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
