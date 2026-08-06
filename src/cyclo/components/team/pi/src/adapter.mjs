import { setTimeout as delay } from "node:timers/promises";

import { Code, ConnectError } from "@connectrpc/connect";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { Modality } from "@cyclo/provider/contract";
import {
  decodePayload,
  encodePayload,
  PI_INFERENCE_FORMAT,
  splitPublicModelId,
} from "@cyclo/provider/protocol";
import { resourceExhaustedRetryAt } from "@cyclo/provider/errors";

const API = "cyclo-pi";
const ZERO_COST = Object.freeze({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
const MAX_SAFE_UINT64 = BigInt(Number.MAX_SAFE_INTEGER);
const MAX_TIMER_DELAY_MS = 2_147_483_647;
const MIN_EXHAUSTION_RETRY_DELAY_MS = 1_000;

export function groupModels(models, { onInvalid = console.warn } = {}) {
  if (!Array.isArray(models)) throw new TypeError("Provider catalogue has no model list");
  if (typeof onInvalid !== "function") throw new TypeError("onInvalid must be a function");
  const groups = new Map();
  const publicIds = new Set();

  for (const portable of models) {
    let provider;
    let id;
    try {
      const split = splitPublicModelId(portable?.id);
      if (!split) throw new TypeError(`model id ${portable?.id} must be PROVIDER/MODEL`);
      ({ provider, model: id } = split);
      if (publicIds.has(portable.id)) throw new TypeError(`duplicate model ${portable.id}`);
      const model = piModel(portable, id);
      publicIds.add(portable.id);
      const group = groups.get(provider) ?? [];
      group.push({ publicId: portable.id, model });
      groups.set(provider, group);
    } catch (error) {
      onInvalid(diagnosticMessage(error));
    }
  }
  return groups;
}

function diagnosticMessage(error) {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, 512) || "invalid provider model";
}

// This is the only Pi-to-wire boundary. Context and inference options are
// serialized once, then every Provider component sees one opaque string.
export function streamProvider(
  client,
  publicId,
  model,
  context,
  options = {},
  { now = Date.now, sleep = abortableSleep } = {},
) {
  const output = createAssistantMessageEventStream();
  void pump(output, client, publicId, model, context, options, now, sleep);
  return output;
}

async function pump(output, client, publicId, model, context, options, now, sleep) {
  try {
    const request = Object.freeze({
      model: publicId,
      payload: encodePayload({ context, options: inferenceOptions(options) }),
    });
    // Relays and poolers get the first chance to handle exhaustion. If it
    // reaches this terminal adapter, keep Pi's stream open and wait outside the
    // completed RPC before replaying the same request.
    while (true) {
      let receivedResponse = false;
      try {
        for await (const response of client.infer(
          request,
          cancellationOptions(options.signal),
        )) {
          receivedResponse = true;
          output.push(decodePayload(response.payload));
        }
        output.end();
        return;
      } catch (error) {
        const retryAt = receivedResponse || options.signal?.aborted
          ? undefined
          : resourceExhaustedRetryAt(error);
        if (retryAt === undefined) throw error;
        const delayMs = Math.max(
          MIN_EXHAUSTION_RETRY_DELAY_MS,
          retryAt.getTime() - now(),
        );
        await sleep(delayMs, options.signal);
        options.signal?.throwIfAborted();
      }
    }
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

async function abortableSleep(delayMs, signal) {
  let remaining = delayMs;
  while (remaining > 0) {
    const chunk = Math.min(remaining, MAX_TIMER_DELAY_MS);
    await delay(chunk, undefined, { signal });
    remaining -= chunk;
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

// Infer has no pipeline-wide deadline. Pi's timeout controls describe its
// native provider request, which is owned by the gateway, and must not become
// an absolute ConnectRPC deadline across every provider component. Operator
// cancellation is the only Pi process control propagated through the call.
function cancellationOptions(signal) {
  return signal === undefined ? {} : { signal };
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
