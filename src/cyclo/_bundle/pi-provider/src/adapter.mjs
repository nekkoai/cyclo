import { Code, ConnectError } from "@connectrpc/connect";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { Modality } from "@cyclo/provider/contract";
import {
  decodePayload,
  encodePayload,
  PI_INFERENCE_FORMAT,
} from "@cyclo/provider/protocol";

const API = "cyclo-pi";
const PROVIDER = /^[a-z0-9_-]+$/u;
const MODEL = /^[^\s\u0000-\u001f\u007f]+$/u;
const RESERVED_PROVIDERS = new Set(["__proto__", "constructor", "gateway", "prototype"]);
const ZERO_COST = Object.freeze({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
const MAX_SAFE_UINT64 = BigInt(Number.MAX_SAFE_INTEGER);

export function groupModels(models) {
  if (!Array.isArray(models)) throw new TypeError("Provider catalogue has no model list");
  const groups = new Map();
  const publicIds = new Set();

  for (const portable of models) {
    const { provider, id } = splitModelId(portable?.id);
    if (publicIds.has(portable.id)) throw new TypeError(`duplicate model ${portable.id}`);
    publicIds.add(portable.id);
    const group = groups.get(provider) ?? [];
    group.push({ publicId: portable.id, model: piModel(portable, id) });
    groups.set(provider, group);
  }
  return groups;
}

// This is the only Pi-to-wire boundary. Context and inference options are
// serialized once, then every Provider component sees one opaque string.
export function streamProvider(client, publicId, model, context, options = {}) {
  const output = createAssistantMessageEventStream();
  void pump(output, client, publicId, model, context, options);
  return output;
}

async function pump(output, client, publicId, model, context, options) {
  try {
    const request = {
      model: publicId,
      payload: encodePayload({ context, options: inferenceOptions(options) }),
    };
    for await (const response of client.infer(request, callOptions(options))) {
      output.push(decodePayload(response.payload));
    }
    output.end();
  } catch (error) {
    const aborted = options.signal?.aborted
      || (error instanceof ConnectError && error.code === Code.Canceled);
    const message = assistantMessage(model);
    message.stopReason = aborted ? "aborted" : "error";
    message.errorMessage = aborted
      ? "Cyclo provider request aborted"
      : `Cyclo provider request failed: ${providerErrorMessage(error)}`;
    output.push({
      type: "error",
      reason: message.stopReason,
      error: message,
    });
  }
}

// These values control the local process or the credential boundary; they are
// not inference data and therefore never enter the payload. All other JSON
// options, including provider-specific options unknown to Cyclo, pass through.
function inferenceOptions(options) {
  const {
    signal: _signal,
    apiKey: _apiKey,
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
  return inference;
}

function callOptions(options) {
  const result = {};
  if (options.signal !== undefined) result.signal = options.signal;
  if (options.timeoutMs !== undefined) result.timeoutMs = options.timeoutMs;
  return result;
}

function providerErrorMessage(error) {
  const raw = error instanceof Error ? error.message : String(error ?? "unknown error");
  const safe = raw.replace(/[\u0000-\u001f\u007f]/gu, " ").replace(/\s+/gu, " ").trim();
  return safe.slice(0, 512) || "unknown provider error";
}

function piModel(portable, id) {
  if (portable?.inferenceFormat !== PI_INFERENCE_FORMAT) {
    throw new TypeError(
      `model ${portable?.id} uses unsupported inference format ${portable?.inferenceFormat || "(missing)"}`,
    );
  }
  const capabilities = portable?.capabilities;
  if (!capabilities || !Array.isArray(capabilities.inputModalities)
      || !Array.isArray(capabilities.outputModalities)) {
    throw new TypeError(`model ${portable?.id} has invalid capabilities`);
  }
  if ((portable.extensions ?? []).length || (capabilities.extensionTypes ?? []).length) {
    throw new TypeError(`model ${portable.id} requires unsupported catalogue extensions`);
  }
  const allowedInput = new Set([Modality.TEXT, Modality.IMAGE]);
  if (!capabilities.inputModalities.includes(Modality.TEXT)
      || capabilities.inputModalities.some((value) => !allowedInput.has(value))) {
    throw new TypeError(`model ${portable.id} has unsupported input modalities`);
  }
  if (capabilities.outputModalities.length !== 1
      || capabilities.outputModalities[0] !== Modality.TEXT) {
    throw new TypeError(`model ${portable.id} has unsupported output modalities`);
  }
  if (portable.displayName !== undefined && typeof portable.displayName !== "string") {
    throw new TypeError(`model ${portable.id} has an invalid display name`);
  }
  return Object.freeze({
    id,
    name: portable.displayName || id,
    reasoning: capabilities.reasoning === true,
    input: Object.freeze(capabilities.inputModalities.includes(Modality.IMAGE)
      ? ["text", "image"]
      : ["text"]),
    cost: ZERO_COST,
    contextWindow: uint64Number(portable.contextWindowTokens, "context window", portable.id),
    maxTokens: uint64Number(portable.maxOutputTokens, "output limit", portable.id),
  });
}

function splitModelId(value) {
  if (typeof value !== "string" || !MODEL.test(value)) {
    throw new TypeError("Provider emitted an invalid model id");
  }
  const slash = value.indexOf("/");
  const provider = slash < 0 ? "" : value.slice(0, slash);
  const id = slash < 0 ? "" : value.slice(slash + 1);
  if (!PROVIDER.test(provider) || RESERVED_PROVIDERS.has(provider)
      || !id || id.length > 1_024 || !MODEL.test(id)) {
    throw new TypeError(`model id ${value} must be PROVIDER/MODEL`);
  }
  return { provider, id };
}

function uint64Number(value, label, model) {
  if (typeof value !== "bigint" || value <= 0n || value > MAX_SAFE_UINT64) {
    throw new TypeError(`model ${model} has no usable ${label}`);
  }
  return Number(value);
}

function assistantMessage(model) {
  return {
    role: "assistant",
    content: [],
    api: API,
    provider: model.provider,
    model: model.id,
    usage: {
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    },
    stopReason: "stop",
    timestamp: Date.now(),
  };
}
