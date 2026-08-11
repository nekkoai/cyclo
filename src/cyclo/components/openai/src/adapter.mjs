import { randomUUID } from "node:crypto";
import { isDeepStrictEqual } from "node:util";

import { Modality } from "@cyclo/provider/contract";
import {
  decodePayload,
  encodePayload,
  PI_INFERENCE_FORMAT,
  splitPublicModelId,
} from "@cyclo/provider/protocol";

const API = "openai-responses";
const OPAQUE_PI_SIGNATURE_PREFIX = "cyclo-pi-signature-v1:";
const MAX_METADATA_ENTRIES = 16;
const MAX_METADATA_KEY_LENGTH = 64;
const MAX_METADATA_VALUE_LENGTH = 512;
const MAX_ERROR_MESSAGE_LENGTH = 1_024;
const MAX_SAFE_UINT64 = BigInt(Number.MAX_SAFE_INTEGER);
const REASONING_EFFORTS = new Set([
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);
const SUPPORTED_INCLUDE = new Set(["reasoning.encrypted_content"]);
const REQUEST_FIELDS = new Set([
  "background",
  "context_management",
  "conversation",
  "include",
  "input",
  "instructions",
  "max_output_tokens",
  "metadata",
  "model",
  "parallel_tool_calls",
  "previous_response_id",
  "prompt",
  "prompt_cache_key",
  "prompt_cache_retention",
  "reasoning",
  "safety_identifier",
  "service_tier",
  "store",
  "stream",
  "stream_options",
  "temperature",
  "text",
  "tool_choice",
  "tools",
  "top_p",
  "truncation",
  "user",
]);
const ZERO_USAGE = Object.freeze({
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 0,
  cost: Object.freeze({
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    total: 0,
  }),
});

export class OpenAIRequestError extends Error {
  constructor(message, {
    status = 400,
    code = "invalid_request_error",
    param = null,
    type = "invalid_request_error",
  } = {}) {
    super(message);
    this.name = "OpenAIRequestError";
    this.status = status;
    this.code = code;
    this.param = param;
    this.type = type;
  }
}

export class OpenAIUpstreamError extends Error {
  constructor(message = "Cyclo Provider returned an invalid inference stream") {
    super(message);
    this.name = "OpenAIUpstreamError";
  }
}

export function openAIModels(models, { onInvalid = () => {} } = {}) {
  const usable = compatibleModels(models, onInvalid);
  return [...usable.values()].map(openAIModel);
}

export function openAIModel(model) {
  const selected = splitPublicModelId(model?.id);
  if (!selected) throw new OpenAIUpstreamError("Provider returned an invalid model ID");
  return Object.freeze({
    id: model.id,
    object: "model",
    created: 0,
    owned_by: selected.provider,
  });
}

export async function listOpenAIModels(client, options = {}) {
  const catalogue = await client.listModels(
    {},
    callOptions(options.signal, options.timeoutMs),
  );
  return {
    object: "list",
    data: openAIModels(catalogue.models ?? [], options),
  };
}

export async function getOpenAIModel(client, id, options = {}) {
  if (typeof id !== "string" || !id) {
    throw requestError("model must be a non-empty string", "model");
  }
  const catalogue = await client.listModels(
    {},
    callOptions(options.signal, options.timeoutMs),
  );
  const model = compatibleModels(catalogue.models ?? [], options.onInvalid).get(id);
  if (!model) {
    throw new OpenAIRequestError(`The model '${safeValue(id)}' does not exist`, {
      status: 404,
      code: "model_not_found",
      param: "model",
    });
  }
  return model;
}

export async function* streamOpenAIResponse(client, document, {
  signal,
  now = Date.now,
  idFactory = defaultIdFactory,
  onInvalid,
} = {}) {
  const envelope = validateEnvelope(document);
  const model = await getOpenAIModel(client, envelope.model, { signal, onInvalid });
  const prepared = prepareOpenAIRequest(envelope, model, { now });
  const state = new OpenAIResponseState(envelope, {
    now,
    idFactory,
  });
  const iterable = client.infer({
    model: envelope.model,
    payload: encodePayload(prepared),
  }, callOptions(signal));
  const iterator = iterable[Symbol.asyncIterator]();

  let first;
  try {
    first = await iterator.next();
  } catch (error) {
    throw error;
  }
  if (first.done) {
    throw new OpenAIUpstreamError("Cyclo Provider ended before its first Pi event");
  }

  yield state.createdEvent();
  yield state.inProgressEvent();
  let terminal = false;
  try {
    let next = first;
    while (!next.done) {
      const event = decodeProviderEvent(next.value?.payload);
      const converted = state.accept(event);
      for (const output of converted) yield output;
      if (state.terminal) {
        terminal = true;
        break;
      }
      next = await iterator.next();
    }
    if (!terminal) {
      for (const output of state.fail(
        "Cyclo Provider ended without a terminal Pi event",
      )) yield output;
    }
  } catch (error) {
    if (signal?.aborted) throw error;
    for (const output of state.fail(providerFailureMessage(error))) yield output;
  } finally {
    if (typeof iterator.return === "function") {
      try {
        await iterator.return();
      } catch {
        // The HTTP result is already terminal. Closing a broken upstream is best effort.
      }
    }
  }
}

export async function createOpenAIResponse(client, document, options = {}) {
  let terminal;
  for await (const event of streamOpenAIResponse(client, document, options)) {
    if (
      event.type === "response.completed"
      || event.type === "response.incomplete"
      || event.type === "response.failed"
    ) {
      terminal = event.response;
    }
  }
  if (!terminal) throw new OpenAIUpstreamError();
  return terminal;
}

export function prepareOpenAIRequest(document, model, { now = Date.now } = {}) {
  const selected = splitPublicModelId(model.id);
  if (!selected) throw new OpenAIUpstreamError("Provider returned an invalid model ID");
  const capabilities = model.capabilities;
  const context = openAIInputContext(document, selected, capabilities, now);
  const options = openAIOptions(document, model);
  return Object.freeze({ context, options });
}

export function compatibleModels(models, onInvalid = () => {}) {
  if (!Array.isArray(models)) throw new TypeError("Provider catalogue has no model list");
  if (typeof onInvalid !== "function") throw new TypeError("onInvalid must be a function");
  const result = new Map();
  for (const model of models) {
    try {
      validateModel(model, result);
      result.set(model.id, model);
    } catch (error) {
      onInvalid(diagnosticMessage(error));
    }
  }
  return result;
}

function validateModel(model, accepted) {
  const selected = splitPublicModelId(model?.id);
  if (!selected) throw new TypeError(`model id ${model?.id} must be PROVIDER/MODEL`);
  if (accepted.has(model.id)) throw new TypeError(`duplicate model ${model.id}`);
  if (model.inferenceFormat !== PI_INFERENCE_FORMAT) {
    throw new TypeError(
      `model ${model.id} uses unsupported inference format `
      + `${model.inferenceFormat || "(missing)"}`,
    );
  }
  const capabilities = model.capabilities;
  if (!capabilities || !Array.isArray(capabilities.inputModalities)
      || !Array.isArray(capabilities.outputModalities)) {
    throw new TypeError(`model ${model.id} has invalid capabilities`);
  }
  if ((model.extensions !== undefined && !Array.isArray(model.extensions))
      || (model.extensions ?? []).length > 0
      || (capabilities.extensionTypes !== undefined
        && !Array.isArray(capabilities.extensionTypes))
      || (capabilities.extensionTypes ?? []).length > 0) {
    throw new TypeError(`model ${model.id} requires unsupported catalogue extensions`);
  }
  const allowedInput = new Set([Modality.TEXT, Modality.IMAGE]);
  if (!capabilities.inputModalities.includes(Modality.TEXT)
      || capabilities.inputModalities.some((value) => !allowedInput.has(value))) {
    throw new TypeError(`model ${model.id} has unsupported input modalities`);
  }
  if (capabilities.outputModalities.length !== 1
      || capabilities.outputModalities[0] !== Modality.TEXT) {
    throw new TypeError(`model ${model.id} has unsupported output modalities`);
  }
  if (model.displayName !== undefined && typeof model.displayName !== "string") {
    throw new TypeError(`model ${model.id} has an invalid display name`);
  }
  validateTokenLimit(model.contextWindowTokens, "context window", model.id);
  validateTokenLimit(model.maxOutputTokens, "output limit", model.id);
}

function validateTokenLimit(value, label, model) {
  if (typeof value !== "bigint" || value <= 0n || value > MAX_SAFE_UINT64) {
    throw new TypeError(`model ${model} has no usable ${label}`);
  }
}

function diagnosticMessage(error) {
  return String(error instanceof Error ? error.message : error)
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, 512) || "invalid provider model";
}

function validateEnvelope(document) {
  if (!plainObject(document)) throw requestError("request body must be a JSON object");
  for (const field of Object.keys(document)) {
    if (!REQUEST_FIELDS.has(field)) {
      throw requestError(`unsupported parameter: ${field}`, field, "unsupported_parameter");
    }
  }
  if (typeof document.model !== "string" || !document.model) {
    throw requestError("model must be a non-empty string", "model");
  }
  if (!Object.hasOwn(document, "input")) {
    throw requestError("input is required", "input");
  }
  if (document.stream !== undefined && document.stream !== null
      && typeof document.stream !== "boolean") {
    throw requestError("stream must be a boolean", "stream");
  }
  rejectNonDefault(document.background, false, "background");
  rejectNonDefault(document.store, false, "store");
  rejectNonDefault(document.conversation, null, "conversation");
  rejectNonDefault(document.previous_response_id, null, "previous_response_id");
  rejectNonDefault(document.prompt, null, "prompt");
  rejectNonDefault(document.context_management, null, "context_management", []);
  rejectNonDefault(document.safety_identifier, undefined, "safety_identifier");
  rejectNonDefault(document.user, undefined, "user");
  rejectNonDefault(document.service_tier, "auto", "service_tier", null);
  rejectNonDefault(document.truncation, "disabled", "truncation", null);
  validateInclude(document.include);
  validateStreamOptions(document.stream, document.stream_options);
  validateText(document.text);
  validateMetadata(document.metadata);
  return document;
}

function rejectNonDefault(value, accepted, param, alternate) {
  if (value === undefined || Object.is(value, accepted) || Object.is(value, alternate)) return;
  if (Array.isArray(value) && Array.isArray(alternate)
      && value.length === 0 && alternate.length === 0) return;
  throw requestError(`${param} is not supported by the stateless Provider bridge`, param,
    "unsupported_parameter");
}

function validateInclude(include) {
  if (include === undefined || include === null) return;
  if (!Array.isArray(include) || include.some((value) => !SUPPORTED_INCLUDE.has(value))) {
    throw requestError(
      "include supports only reasoning.encrypted_content",
      "include",
      "unsupported_parameter",
    );
  }
}

function validateStreamOptions(stream, options) {
  if (options === undefined || options === null) return;
  if (!plainObject(options)) throw requestError("stream_options must be an object", "stream_options");
  if (stream !== true) {
    throw requestError("stream_options requires stream=true", "stream_options");
  }
  const fields = Object.keys(options);
  if (fields.some((field) => field !== "include_obfuscation")) {
    throw requestError("stream_options contains an unsupported parameter", "stream_options",
      "unsupported_parameter");
  }
  if (options.include_obfuscation !== undefined && options.include_obfuscation !== false) {
    throw requestError("stream obfuscation is not supported", "stream_options.include_obfuscation",
      "unsupported_parameter");
  }
}

function validateText(text) {
  if (text === undefined || text === null) return;
  if (!plainObject(text)) throw requestError("text must be an object", "text");
  const format = text.format;
  const plain = format === undefined
    || (plainObject(format) && format.type === "text" && Object.keys(format).length === 1);
  if (!plain || (text.verbosity !== undefined && text.verbosity !== null)) {
    throw requestError("only plain text output is supported", "text", "unsupported_parameter");
  }
  if (Object.keys(text).some((field) => !["format", "verbosity"].includes(field))) {
    throw requestError("text contains an unsupported parameter", "text", "unsupported_parameter");
  }
}

function validateMetadata(metadata) {
  if (metadata === undefined || metadata === null) return;
  if (!plainObject(metadata)) throw requestError("metadata must be an object", "metadata");
  const entries = Object.entries(metadata);
  if (entries.length > MAX_METADATA_ENTRIES) {
    throw requestError("metadata has more than 16 entries", "metadata");
  }
  for (const [key, value] of entries) {
    if (!key || key.length > MAX_METADATA_KEY_LENGTH || typeof value !== "string"
        || value.length > MAX_METADATA_VALUE_LENGTH) {
      throw requestError("metadata keys or values are invalid", "metadata");
    }
  }
}

function openAIInputContext(document, selected, capabilities, now) {
  const messages = [];
  const system = [];
  const toolNames = new Map();
  let pendingAssistant;

  const flushAssistant = () => {
    if (!pendingAssistant || pendingAssistant.content.length === 0) return;
    messages.push(pendingAssistant);
    pendingAssistant = undefined;
  };
  const assistant = () => {
    pendingAssistant ??= assistantHistoryMessage(selected, now());
    return pendingAssistant;
  };

  if (document.instructions !== undefined && document.instructions !== null) {
    if (typeof document.instructions !== "string") {
      throw requestError("instructions must be a string", "instructions");
    }
    if (document.instructions) system.push(document.instructions);
  }

  const input = document.input === undefined
    ? []
    : typeof document.input === "string"
      ? [{ role: "user", content: document.input }]
      : document.input;
  if (!Array.isArray(input)) throw requestError("input must be a string or array", "input");

  for (let index = 0; index < input.length; index += 1) {
    const item = input[index];
    if (!plainObject(item)) throw requestError("input items must be objects", `input[${index}]`);
    const type = item.type ?? (typeof item.role === "string" ? "message" : undefined);
    if (type === "message") {
      const role = item.role;
      if (["system", "developer"].includes(role)) {
        if (messages.length > 0 || pendingAssistant) {
          throw requestError(
            "system and developer messages must precede conversation history",
            `input[${index}].role`,
          );
        }
        const text = inputText(item.content, `input[${index}].content`);
        if (text) system.push(text);
      } else if (role === "user") {
        flushAssistant();
        messages.push({
          role: "user",
          content: userContent(item.content, capabilities, `input[${index}].content`),
          timestamp: now(),
        });
      } else if (role === "assistant") {
        for (const content of assistantContent(
          item.content,
          `input[${index}].content`,
          item.id,
          item.phase,
        )) {
          assistant().content.push(content);
        }
      } else {
        throw requestError("message role is unsupported", `input[${index}].role`);
      }
    } else if (type === "reasoning") {
      const reasoning = reasoningHistory(item, `input[${index}]`);
      assistant().content.push(reasoning);
    } else if (type === "function_call") {
      const call = functionCallHistory(item, `input[${index}]`);
      assistant().content.push(call.block);
      toolNames.set(call.callId, call.name);
    } else if (type === "function_call_output") {
      flushAssistant();
      const callId = requiredString(item.call_id, `input[${index}].call_id`);
      const name = toolNames.get(callId);
      if (!name) {
        throw requestError(
          "function_call_output has no preceding function_call in this stateless request",
          `input[${index}].call_id`,
        );
      }
      messages.push({
        role: "toolResult",
        toolCallId: callId,
        toolName: name,
        content: toolOutputContent(item.output, capabilities, `input[${index}].output`),
        isError: item.status === "failed",
        timestamp: now(),
      });
    } else {
      throw requestError(`unsupported input item type: ${safeValue(type)}`, `input[${index}].type`,
        "unsupported_parameter");
    }
  }
  flushAssistant();

  const tools = openAITools(document.tools, capabilities);
  return Object.freeze({
    ...(system.length > 0 ? { systemPrompt: system.join("\n\n") } : {}),
    messages,
    ...(tools.length > 0 && document.tool_choice !== "none" ? { tools } : {}),
  });
}

function openAIOptions(document, model) {
  const capabilities = model.capabilities;
  const options = {};
  if (document.max_output_tokens !== undefined && document.max_output_tokens !== null) {
    if (!Number.isSafeInteger(document.max_output_tokens) || document.max_output_tokens <= 0) {
      throw requestError("max_output_tokens must be a positive integer", "max_output_tokens");
    }
    if (BigInt(document.max_output_tokens) > model.maxOutputTokens) {
      throw requestError(
        "max_output_tokens exceeds the selected model's output limit",
        "max_output_tokens",
      );
    }
    options.maxTokens = document.max_output_tokens;
  }
  if (document.temperature !== undefined && document.temperature !== null) {
    if (!capabilities?.temperature) {
      throw requestError("the selected model does not support temperature", "temperature");
    }
    if (typeof document.temperature !== "number" || !Number.isFinite(document.temperature)
        || document.temperature < 0 || document.temperature > 2) {
      throw requestError("temperature must be between 0 and 2", "temperature");
    }
    options.temperature = document.temperature;
  }
  if (document.top_p !== undefined && document.top_p !== null) {
    if (!capabilities?.topP) {
      throw requestError("the selected model does not support top_p", "top_p");
    }
    if (typeof document.top_p !== "number" || !Number.isFinite(document.top_p)
        || document.top_p <= 0 || document.top_p > 1) {
      throw requestError("top_p must be greater than 0 and at most 1", "top_p");
    }
    options.samplingParams = { top_p: document.top_p };
  }
  if (document.parallel_tool_calls === false) {
    throw requestError(
      "parallel_tool_calls=false cannot be enforced by the Pi Provider ABI",
      "parallel_tool_calls",
      "unsupported_parameter",
    );
  }
  const toolChoice = document.tool_choice;
  if (toolChoice !== undefined && toolChoice !== null
      && toolChoice !== "auto" && toolChoice !== "none") {
    throw requestError("only tool_choice auto or none is supported", "tool_choice",
      "unsupported_parameter");
  }
  if (document.metadata !== undefined && document.metadata !== null) {
    options.metadata = { ...document.metadata };
  }
  if (document.prompt_cache_key !== undefined) {
    if (typeof document.prompt_cache_key !== "string" || !document.prompt_cache_key) {
      throw requestError("prompt_cache_key must be a non-empty string", "prompt_cache_key");
    }
    options.sessionId = document.prompt_cache_key;
  }
  if (document.prompt_cache_retention !== undefined
      && document.prompt_cache_retention !== null) {
    if (document.prompt_cache_retention === "24h") options.cacheRetention = "long";
    else if (document.prompt_cache_retention === "in-memory") options.cacheRetention = "short";
    else throw requestError("prompt_cache_retention is invalid", "prompt_cache_retention");
  }
  if (document.reasoning !== undefined && document.reasoning !== null) {
    if (!capabilities?.reasoning) {
      throw requestError("the selected model does not support reasoning", "reasoning");
    }
    if (!plainObject(document.reasoning)) throw requestError("reasoning must be an object", "reasoning");
    const fields = Object.keys(document.reasoning);
    if (fields.some((field) => !["effort", "summary", "generate_summary"].includes(field))) {
      throw requestError("reasoning contains an unsupported parameter", "reasoning",
        "unsupported_parameter");
    }
    const effort = document.reasoning.effort;
    if (effort !== undefined && effort !== null && !REASONING_EFFORTS.has(effort)) {
      throw requestError("reasoning.effort is invalid", "reasoning.effort");
    }
    const summary = document.reasoning.summary ?? document.reasoning.generate_summary;
    if (summary !== undefined && summary !== null && summary !== "auto") {
      throw requestError("only reasoning summary auto is supported", "reasoning.summary",
        "unsupported_parameter");
    }
    if (effort !== undefined && effort !== null && effort !== "none") options.reasoning = effort;
  }
  return Object.freeze(options);
}

function openAITools(tools, capabilities) {
  if (tools === undefined || tools === null) return [];
  if (!Array.isArray(tools)) throw requestError("tools must be an array", "tools");
  if (tools.length > 0 && !capabilities?.functionTools) {
    throw requestError("the selected model does not support function tools", "tools");
  }
  const names = new Set();
  return tools.map((tool, index) => {
    const param = `tools[${index}]`;
    if (!plainObject(tool) || tool.type !== "function") {
      throw requestError("only function tools are supported", param, "unsupported_parameter");
    }
    const name = requiredString(tool.name, `${param}.name`);
    if (names.has(name)) throw requestError(`duplicate tool name: ${safeValue(name)}`, `${param}.name`);
    names.add(name);
    if (tool.description !== undefined && typeof tool.description !== "string") {
      throw requestError("tool description must be a string", `${param}.description`);
    }
    const parameters = tool.parameters ?? { type: "object", properties: {} };
    if (!plainObject(parameters)) {
      throw requestError("tool parameters must be a JSON Schema object", `${param}.parameters`);
    }
    if (tool.strict !== undefined && typeof tool.strict !== "boolean") {
      throw requestError("tool strict must be a boolean", `${param}.strict`);
    }
    const allowed = new Set(["type", "name", "description", "parameters", "strict", "defer_loading"]);
    if (Object.keys(tool).some((field) => !allowed.has(field)) || tool.defer_loading === true) {
      throw requestError("tool contains an unsupported parameter", param, "unsupported_parameter");
    }
    return {
      name,
      description: tool.description ?? "",
      parameters,
      ...(tool.strict === true
        ? { constrainedSampling: { type: "json_schema", strict: "require" } }
        : {}),
    };
  });
}

function userContent(content, capabilities, param) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) throw requestError("message content must be a string or array", param);
  const result = [];
  for (let index = 0; index < content.length; index += 1) {
    const part = content[index];
    if (!plainObject(part)) throw requestError("message content parts must be objects", `${param}[${index}]`);
    if (["input_text", "text"].includes(part.type)) {
      rejectExtraFields(part, ["type", "text"], `${param}[${index}]`);
      result.push({ type: "text", text: requiredString(part.text, `${param}[${index}].text`, true) });
    } else if (part.type === "input_image") {
      rejectExtraFields(part, ["type", "image_url", "detail"], `${param}[${index}]`);
      if (part.detail !== undefined && part.detail !== null && part.detail !== "auto") {
        throw requestError(
          "only automatic image detail is supported",
          `${param}[${index}].detail`,
          "unsupported_parameter",
        );
      }
      if (!capabilities?.inputModalities?.includes(Modality.IMAGE)) {
        throw requestError("the selected model does not support image input", `${param}[${index}]`);
      }
      result.push(dataImage(part.image_url, `${param}[${index}].image_url`));
    } else {
      throw requestError("only input_text and data-URL input_image parts are supported",
        `${param}[${index}].type`, "unsupported_parameter");
    }
  }
  return result;
}

function inputText(content, param) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) throw requestError("message content must be a string or array", param);
  return content.map((part, index) => {
    if (!plainObject(part) || !["input_text", "text"].includes(part.type)) {
      throw requestError("system content supports text only", `${param}[${index}]`,
        "unsupported_parameter");
    }
    rejectExtraFields(part, ["type", "text"], `${param}[${index}]`);
    return requiredString(part.text, `${param}[${index}].text`, true);
  }).join("\n");
}

function assistantContent(content, param, itemId, phase) {
  if (typeof content === "string") {
    return [{
      type: "text",
      text: content,
      ...(typeof itemId === "string" && itemId
        ? { textSignature: textSignature(itemId, phase) }
        : {}),
    }];
  }
  if (!Array.isArray(content)) throw requestError("assistant content must be a string or array", param);
  const text = content.map((part, index) => {
    if (!plainObject(part) || !["output_text", "text", "refusal"].includes(part.type)) {
      throw requestError("assistant content supports output_text or refusal only",
        `${param}[${index}]`, "unsupported_parameter");
    }
    rejectExtraFields(
      part,
      part.type === "refusal"
        ? ["type", "refusal"]
        : ["type", "text", "annotations", "logprobs"],
      `${param}[${index}]`,
    );
    return part.type === "refusal"
      ? requiredString(part.refusal, `${param}[${index}].refusal`, true)
      : requiredString(part.text, `${param}[${index}].text`, true);
  }).join("");
  return [{
    type: "text",
    text,
    ...(typeof itemId === "string" && itemId
      ? { textSignature: textSignature(itemId, phase) }
      : {}),
  }];
}

function reasoningHistory(item, param) {
  if (item.type !== "reasoning") throw requestError("reasoning item is invalid", param);
  const summary = Array.isArray(item.summary)
    ? item.summary.map((part, index) => {
      if (!plainObject(part) || part.type !== "summary_text") {
        throw requestError("reasoning summary is invalid", `${param}.summary[${index}]`);
      }
      return requiredString(part.text, `${param}.summary[${index}].text`, true);
    }).join("\n\n")
    : "";
  const content = Array.isArray(item.content)
    ? item.content.map((part, index) => {
      if (!plainObject(part) || part.type !== "reasoning_text") {
        throw requestError("reasoning content is invalid", `${param}.content[${index}]`);
      }
      return requiredString(part.text, `${param}.content[${index}].text`, true);
    }).join("\n\n")
    : "";
  if (item.encrypted_content !== undefined && item.encrypted_content !== null
      && typeof item.encrypted_content !== "string") {
    throw requestError("reasoning encrypted_content must be a string", `${param}.encrypted_content`);
  }
  const opaque = decodeOpaquePiSignature(item.encrypted_content, `${param}.encrypted_content`);
  return {
    type: "thinking",
    thinking: summary || content,
    thinkingSignature: opaque ?? JSON.stringify(item),
  };
}

function functionCallHistory(item, param) {
  const callId = requiredString(item.call_id, `${param}.call_id`);
  const name = requiredString(item.name, `${param}.name`);
  const itemId = typeof item.id === "string" && item.id ? item.id : `fc_${callId}`;
  if (typeof item.arguments !== "string") {
    throw requestError("function call arguments must be a JSON string", `${param}.arguments`);
  }
  let args;
  try {
    args = JSON.parse(item.arguments);
  } catch {
    throw requestError("function call arguments are not valid JSON", `${param}.arguments`);
  }
  if (!plainObject(args)) {
    throw requestError("function call arguments must decode to an object", `${param}.arguments`);
  }
  return {
    callId,
    name,
    block: { type: "toolCall", id: `${callId}|${itemId}`, name, arguments: args },
  };
}

function toolOutputContent(output, capabilities, param) {
  if (typeof output === "string") return [{ type: "text", text: output }];
  if (!Array.isArray(output)) throw requestError("function output must be a string or array", param);
  const content = [];
  for (let index = 0; index < output.length; index += 1) {
    const part = output[index];
    if (!plainObject(part)) throw requestError("function output parts must be objects", `${param}[${index}]`);
    if (["input_text", "text"].includes(part.type)) {
      rejectExtraFields(part, ["type", "text"], `${param}[${index}]`);
      content.push({ type: "text", text: requiredString(part.text, `${param}[${index}].text`, true) });
    } else if (part.type === "input_image") {
      rejectExtraFields(part, ["type", "image_url", "detail"], `${param}[${index}]`);
      if (part.detail !== undefined && part.detail !== null && part.detail !== "auto") {
        throw requestError(
          "only automatic image detail is supported",
          `${param}[${index}].detail`,
          "unsupported_parameter",
        );
      }
      if (!capabilities?.inputModalities?.includes(Modality.IMAGE)) {
        throw requestError("the selected model does not support image input", `${param}[${index}]`);
      }
      content.push(dataImage(part.image_url, `${param}[${index}].image_url`));
    } else {
      throw requestError("function output supports input_text and data-URL input_image only",
        `${param}[${index}].type`, "unsupported_parameter");
    }
  }
  return content;
}

function dataImage(value, param) {
  if (typeof value !== "string") throw requestError("image_url must be a data URL", param);
  const match = /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})$/iu.exec(value);
  if (!match || !match[2] || match[2].length % 4 !== 0) {
    throw new OpenAIRequestError("only valid base64 data-URL images are supported", {
      code: "invalid_image_url",
      param,
    });
  }
  const data = Buffer.from(match[2], "base64");
  if (data.toString("base64") !== match[2]) {
    throw new OpenAIRequestError("image_url contains invalid base64 data", {
      code: "invalid_base64_image",
      param,
    });
  }
  return { type: "image", mimeType: match[1].toLowerCase(), data: match[2] };
}

function assistantHistoryMessage(selected, timestamp) {
  return {
    role: "assistant",
    content: [],
    api: API,
    provider: selected.provider,
    model: selected.model,
    usage: clone(ZERO_USAGE),
    stopReason: "stop",
    timestamp,
  };
}

class OpenAIResponseState {
  constructor(request, { now, idFactory }) {
    this.request = request;
    this.now = now;
    this.idFactory = idFactory;
    this.id = idFactory("resp");
    this.createdAt = Math.floor(now() / 1_000);
    this.sequence = 0;
    this.output = [];
    this.slots = new Map();
    this.started = false;
    this.terminal = false;
    this.usage = undefined;
    this.outputText = "";
    this.includeEncryptedReasoning = request.include?.includes(
      "reasoning.encrypted_content",
    ) === true;
  }

  createdEvent() {
    return this.event("response.created", {
      response: this.response("in_progress"),
    });
  }

  inProgressEvent() {
    return this.event("response.in_progress", {
      response: this.response("in_progress"),
    });
  }

  accept(event) {
    if (this.terminal) throw new OpenAIUpstreamError("Provider emitted an event after termination");
    if (!plainObject(event) || typeof event.type !== "string") {
      throw new OpenAIUpstreamError("Provider emitted an invalid Pi event");
    }
    if (event.type === "start") {
      if (this.started) throw new OpenAIUpstreamError("Provider emitted more than one Pi start event");
      this.started = true;
      return [];
    }
    if (!this.started) throw new OpenAIUpstreamError("Provider emitted Pi content before start");
    if (event.type === "text_start") return this.startText(event);
    if (event.type === "text_delta") return this.deltaText(event);
    if (event.type === "text_end") return this.endText(event);
    if (event.type === "thinking_start") return this.startThinking(event);
    if (event.type === "thinking_delta") return this.deltaThinking(event);
    if (event.type === "thinking_end") return this.endThinking(event);
    if (event.type === "toolcall_start") return this.startToolCall(event);
    if (event.type === "toolcall_delta") return this.deltaToolCall(event);
    if (event.type === "toolcall_end") return this.endToolCall(event);
    if (event.type === "done") return this.done(event);
    if (event.type === "error") return this.error(event);
    throw new OpenAIUpstreamError(`Provider emitted unsupported Pi event ${safeValue(event.type)}`);
  }

  startText(event) {
    const index = contentIndex(event);
    this.requireNewSlot(index);
    const signature = textSignatureParts(event.partial?.content?.[index]?.textSignature);
    const item = {
      id: signature?.id || this.idFactory("msg"),
      type: "message",
      status: "in_progress",
      role: "assistant",
      content: [],
      ...(signature?.phase ? { phase: signature.phase } : {}),
    };
    const slot = this.addSlot(index, "text", item, { text: "" });
    return [
      this.outputAdded(slot),
      this.event("response.content_part.added", {
        item_id: item.id,
        output_index: slot.outputIndex,
        content_index: 0,
        part: { type: "output_text", text: "", annotations: [], logprobs: [] },
      }),
    ];
  }

  deltaText(event) {
    const slot = this.requireSlot(contentIndex(event), "text");
    if (typeof event.delta !== "string") throw new OpenAIUpstreamError("Pi text delta is not a string");
    slot.buffer.text += event.delta;
    return [this.event("response.output_text.delta", {
      item_id: slot.item.id,
      output_index: slot.outputIndex,
      content_index: 0,
      delta: event.delta,
      logprobs: [],
    })];
  }

  endText(event) {
    const slot = this.requireSlot(contentIndex(event), "text");
    if (typeof event.content !== "string") throw new OpenAIUpstreamError("Pi text terminal is not a string");
    const events = [];
    if (event.content !== slot.buffer.text) {
      if (!event.content.startsWith(slot.buffer.text)) {
        throw new OpenAIUpstreamError("Pi text terminal disagrees with streamed deltas");
      }
      events.push(...this.deltaText({
        contentIndex: slot.contentIndex,
        delta: event.content.slice(slot.buffer.text.length),
      }));
    }
    events.push(...this.finishText(slot, event.partial?.content?.[slot.contentIndex]));
    return events;
  }

  startThinking(event) {
    const index = contentIndex(event);
    this.requireNewSlot(index);
    const signature = reasoningSignature(event.partial?.content?.[index]?.thinkingSignature);
    const item = {
      id: signature?.id || this.idFactory("rs"),
      type: "reasoning",
      summary: [],
      status: "in_progress",
      ...(this.includeEncryptedReasoning && signature?.encrypted_content
        ? { encrypted_content: signature.encrypted_content }
        : {}),
    };
    const slot = this.addSlot(index, "thinking", item, { text: "" });
    return [
      this.outputAdded(slot),
      this.event("response.reasoning_summary_part.added", {
        item_id: item.id,
        output_index: slot.outputIndex,
        summary_index: 0,
        part: { type: "summary_text", text: "" },
      }),
    ];
  }

  deltaThinking(event) {
    const slot = this.requireSlot(contentIndex(event), "thinking");
    if (typeof event.delta !== "string") throw new OpenAIUpstreamError("Pi thinking delta is not a string");
    slot.buffer.text += event.delta;
    return [this.event("response.reasoning_summary_text.delta", {
      item_id: slot.item.id,
      output_index: slot.outputIndex,
      summary_index: 0,
      delta: event.delta,
    })];
  }

  endThinking(event) {
    const slot = this.requireSlot(contentIndex(event), "thinking");
    if (typeof event.content !== "string") throw new OpenAIUpstreamError("Pi thinking terminal is not a string");
    const events = [];
    if (event.content !== slot.buffer.text) {
      if (!event.content.startsWith(slot.buffer.text)) {
        throw new OpenAIUpstreamError("Pi thinking terminal disagrees with streamed deltas");
      }
      events.push(...this.deltaThinking({
        contentIndex: slot.contentIndex,
        delta: event.content.slice(slot.buffer.text.length),
      }));
    }
    events.push(...this.finishThinking(slot, event.partial?.content?.[slot.contentIndex]));
    return events;
  }

  startToolCall(event) {
    const index = contentIndex(event);
    this.requireNewSlot(index);
    const block = event.partial?.content?.[index];
    if (!plainObject(block) || block.type !== "toolCall"
        || typeof block.name !== "string" || !block.name) {
      throw new OpenAIUpstreamError("Pi tool start is invalid");
    }
    const ids = toolCallIds(block.id);
    const item = {
      id: ids.itemId ?? this.idFactory("fc"),
      type: "function_call",
      status: "in_progress",
      call_id: ids.callId,
      name: block.name,
      arguments: "",
    };
    const slot = this.addSlot(index, "toolCall", item, { json: "" });
    return [this.outputAdded(slot)];
  }

  deltaToolCall(event) {
    const slot = this.requireSlot(contentIndex(event), "toolCall");
    if (typeof event.delta !== "string") throw new OpenAIUpstreamError("Pi tool delta is not a string");
    slot.buffer.json += event.delta;
    return [this.event("response.function_call_arguments.delta", {
      item_id: slot.item.id,
      output_index: slot.outputIndex,
      delta: event.delta,
    })];
  }

  endToolCall(event) {
    const slot = this.requireSlot(contentIndex(event), "toolCall");
    const block = event.toolCall;
    if (!plainObject(block) || !plainObject(block.arguments) || typeof block.name !== "string") {
      throw new OpenAIUpstreamError("Pi tool terminal is invalid");
    }
    const ids = toolCallIds(block.id);
    if (ids.callId !== slot.item.call_id
        || (ids.itemId !== undefined && ids.itemId !== slot.item.id)) {
      throw new OpenAIUpstreamError("Pi tool call identity changed while streaming");
    }
    if (!block.name || block.name !== slot.item.name) {
      throw new OpenAIUpstreamError("Pi tool call name changed while streaming");
    }
    const canonical = JSON.stringify(block.arguments);
    const events = [];
    let json = canonical;
    if (slot.buffer.json) {
      try {
        const parsed = JSON.parse(slot.buffer.json);
        if (isDeepStrictEqual(parsed, block.arguments)) json = slot.buffer.json;
      } catch {
        // An argument stream is normally incomplete until its terminal event.
      }
    }
    if (json !== slot.buffer.json) {
      if (!json.startsWith(slot.buffer.json)) {
        throw new OpenAIUpstreamError(
          "Pi tool terminal disagrees with streamed argument deltas",
        );
      }
      events.push(...this.deltaToolCall({
        contentIndex: slot.contentIndex,
        delta: json.slice(slot.buffer.json.length),
      }));
    }
    slot.item.arguments = json;
    slot.item.status = "completed";
    events.push(this.event("response.function_call_arguments.done", {
      item_id: slot.item.id,
      output_index: slot.outputIndex,
      name: slot.item.name,
      arguments: json,
    }));
    events.push(this.outputDone(slot));
    slot.done = true;
    return events;
  }

  done(event) {
    if (!plainObject(event.message)) throw new OpenAIUpstreamError("Pi done event has no message");
    const events = this.reconcile(event.message);
    this.usage = responseUsage(event.message.usage);
    const reason = event.reason ?? event.message.stopReason;
    this.terminal = true;
    if (reason === "deferred") {
      return [...events, ...this.fail("Deferred Pi responses are not supported")];
    }
    if (reason === "length") {
      events.push(this.event("response.incomplete", {
        response: this.response("incomplete", {
          incomplete_details: { reason: "max_output_tokens" },
        }),
      }));
      return events;
    }
    if (!["stop", "toolUse"].includes(reason)) {
      throw new OpenAIUpstreamError(`Pi done event has invalid reason ${safeValue(reason)}`);
    }
    events.push(this.event("response.completed", {
      response: this.response("completed", {
        completed_at: Math.floor(this.now() / 1_000),
      }),
    }));
    return events;
  }

  error(event) {
    const message = plainObject(event.error) ? event.error : {};
    const events = this.reconcile(message);
    this.usage = responseUsage(message.usage);
    return [...events, ...this.fail(safeErrorMessage(
      message.errorMessage ?? "Cyclo Provider inference failed",
    ))];
  }

  fail(message) {
    if (this.terminal && this.failureEmitted) return [];
    this.terminal = true;
    this.failureEmitted = true;
    return [this.event("response.failed", {
      response: this.response("failed", {
        error: { code: "server_error", message: safeErrorMessage(message) },
      }),
    })];
  }

  reconcile(message) {
    const events = [];
    const content = Array.isArray(message.content) ? message.content : [];
    for (let index = 0; index < content.length; index += 1) {
      const block = content[index];
      let slot = this.slots.get(index);
      if (!slot) {
        if (block?.type === "text") events.push(...this.startText({
          contentIndex: index,
          partial: message,
        }));
        else if (block?.type === "thinking") events.push(...this.startThinking({
          contentIndex: index,
          partial: message,
        }));
        else if (block?.type === "toolCall") events.push(...this.startToolCall({
          contentIndex: index,
          partial: message,
        }));
        else throw new OpenAIUpstreamError("Pi terminal contains unsupported content");
        slot = this.slots.get(index);
      }
      if (slot.done) {
        this.validateFinishedSlot(slot, block);
        continue;
      }
      if (block.type === "text" && slot.kind === "text") {
        if (block.text.startsWith(slot.buffer.text) && block.text.length > slot.buffer.text.length) {
          events.push(...this.deltaText({ contentIndex: index, delta: block.text.slice(slot.buffer.text.length) }));
        }
        events.push(...this.endText({ contentIndex: index, content: block.text, partial: message }));
      } else if (block.type === "thinking" && slot.kind === "thinking") {
        if (block.thinking.startsWith(slot.buffer.text)
            && block.thinking.length > slot.buffer.text.length) {
          events.push(...this.deltaThinking({
            contentIndex: index,
            delta: block.thinking.slice(slot.buffer.text.length),
          }));
        }
        events.push(...this.endThinking({ contentIndex: index, content: block.thinking, partial: message }));
      } else if (block.type === "toolCall" && slot.kind === "toolCall") {
        events.push(...this.endToolCall({ contentIndex: index, toolCall: block }));
      } else {
        throw new OpenAIUpstreamError("Pi terminal content changed type while streaming");
      }
    }
    if (this.slots.size !== content.length
        || [...this.slots.values()].some((slot) => !slot.done)) {
      throw new OpenAIUpstreamError("Pi terminal omitted streamed content");
    }
    return events;
  }

  validateFinishedSlot(slot, block) {
    if (slot.kind === "text"
        && (block?.type !== "text" || block.text !== slot.buffer.text)) {
      throw new OpenAIUpstreamError("Pi terminal changed completed text content");
    }
    if (slot.kind === "thinking"
        && (block?.type !== "thinking" || block.thinking !== slot.buffer.text)) {
      throw new OpenAIUpstreamError("Pi terminal changed completed reasoning content");
    }
    if (slot.kind === "toolCall") {
      if (block?.type !== "toolCall" || block.name !== slot.item.name
          || !isDeepStrictEqual(block.arguments, JSON.parse(slot.item.arguments))) {
        throw new OpenAIUpstreamError("Pi terminal changed a completed tool call");
      }
      const ids = toolCallIds(block.id);
      if (ids.callId !== slot.item.call_id
          || (ids.itemId !== undefined && ids.itemId !== slot.item.id)) {
        throw new OpenAIUpstreamError("Pi terminal changed a completed tool call identity");
      }
    }
  }

  finishText(slot, block) {
    const signature = textSignatureParts(block?.textSignature);
    if (signature?.phase) slot.item.phase = signature.phase;
    const part = {
      type: "output_text",
      text: slot.buffer.text,
      annotations: [],
      logprobs: [],
    };
    slot.item.content = [part];
    slot.item.status = "completed";
    this.outputText += slot.buffer.text;
    slot.done = true;
    return [
      this.event("response.output_text.done", {
        item_id: slot.item.id,
        output_index: slot.outputIndex,
        content_index: 0,
        text: slot.buffer.text,
        logprobs: [],
      }),
      this.event("response.content_part.done", {
        item_id: slot.item.id,
        output_index: slot.outputIndex,
        content_index: 0,
        part: clone(part),
      }),
      this.outputDone(slot),
    ];
  }

  finishThinking(slot, block) {
    const signature = reasoningSignature(block?.thinkingSignature);
    if (this.includeEncryptedReasoning && signature?.encrypted_content) {
      slot.item.encrypted_content = signature.encrypted_content;
    }
    const part = { type: "summary_text", text: slot.buffer.text };
    slot.item.summary = [part];
    slot.item.status = "completed";
    slot.done = true;
    return [
      this.event("response.reasoning_summary_text.done", {
        item_id: slot.item.id,
        output_index: slot.outputIndex,
        summary_index: 0,
        text: slot.buffer.text,
      }),
      this.event("response.reasoning_summary_part.done", {
        item_id: slot.item.id,
        output_index: slot.outputIndex,
        summary_index: 0,
        part: clone(part),
      }),
      this.outputDone(slot),
    ];
  }

  addSlot(contentIndexValue, kind, item, buffer) {
    const slot = {
      contentIndex: contentIndexValue,
      outputIndex: this.output.length,
      kind,
      item,
      buffer,
      done: false,
    };
    this.output.push(item);
    this.slots.set(contentIndexValue, slot);
    return slot;
  }

  requireNewSlot(index) {
    if (this.slots.has(index)) throw new OpenAIUpstreamError("Pi content started more than once");
  }

  requireSlot(index, kind) {
    const slot = this.slots.get(index);
    if (!slot || slot.kind !== kind || slot.done) {
      throw new OpenAIUpstreamError(`Pi ${kind} event has no active content slot`);
    }
    return slot;
  }

  outputAdded(slot) {
    return this.event("response.output_item.added", {
      output_index: slot.outputIndex,
      item: clone(slot.item),
    });
  }

  outputDone(slot) {
    return this.event("response.output_item.done", {
      output_index: slot.outputIndex,
      item: clone(slot.item),
    });
  }

  response(status, overrides = {}) {
    return {
      id: this.id,
      object: "response",
      created_at: this.createdAt,
      status,
      completed_at: null,
      error: null,
      incomplete_details: null,
      instructions: this.request.instructions ?? null,
      max_output_tokens: this.request.max_output_tokens ?? null,
      model: this.request.model,
      output: clone(this.output),
      output_text: this.outputText,
      parallel_tool_calls: this.request.parallel_tool_calls ?? true,
      previous_response_id: null,
      reasoning: this.request.reasoning ?? null,
      store: false,
      temperature: this.request.temperature ?? null,
      text: this.request.text ?? { format: { type: "text" } },
      tool_choice: this.request.tool_choice ?? "auto",
      tools: this.request.tools ?? [],
      top_p: this.request.top_p ?? null,
      truncation: "disabled",
      usage: this.usage ?? null,
      metadata: this.request.metadata ?? {},
      ...overrides,
    };
  }

  event(type, fields) {
    return { type, ...fields, sequence_number: this.sequence++ };
  }
}

function decodeProviderEvent(payload) {
  let event;
  try {
    event = decodePayload(payload);
  } catch {
    throw new OpenAIUpstreamError("Provider returned an invalid Pi event payload");
  }
  return event;
}

function responseUsage(usage) {
  if (!plainObject(usage)) return undefined;
  const input = tokens(usage.input);
  const cacheRead = tokens(usage.cacheRead);
  const cacheWrite = tokens(usage.cacheWrite);
  const output = tokens(usage.output);
  const total = tokens(usage.totalTokens) || input + cacheRead + cacheWrite + output;
  return {
    input_tokens: input + cacheRead + cacheWrite,
    input_tokens_details: {
      cached_tokens: cacheRead,
      ...(cacheWrite > 0 ? { cache_write_tokens: cacheWrite } : {}),
    },
    output_tokens: output,
    output_tokens_details: { reasoning_tokens: tokens(usage.reasoning) },
    total_tokens: total,
  };
}

function tokens(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function reasoningSignature(value) {
  if (typeof value !== "string" || !value) return undefined;
  if (value.startsWith("{")) {
    try {
      const item = JSON.parse(value);
      if (plainObject(item) && item.type === "reasoning") return item;
    } catch {
      // Provider-specific signatures may themselves begin with a brace.
    }
  }
  return {
    encrypted_content: OPAQUE_PI_SIGNATURE_PREFIX
      + Buffer.from(value, "utf8").toString("base64url"),
  };
}

function decodeOpaquePiSignature(value, param) {
  if (typeof value !== "string" || !value.startsWith(OPAQUE_PI_SIGNATURE_PREFIX)) {
    return undefined;
  }
  const encoded = value.slice(OPAQUE_PI_SIGNATURE_PREFIX.length);
  if (!encoded || !/^[A-Za-z0-9_-]+$/u.test(encoded)) {
    throw requestError("reasoning encrypted_content is invalid", param);
  }
  const decoded = Buffer.from(encoded, "base64url");
  if (decoded.toString("base64url") !== encoded) {
    throw requestError("reasoning encrypted_content is invalid", param);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(decoded);
  } catch {
    throw requestError("reasoning encrypted_content is invalid", param);
  }
}

function textSignatureParts(value) {
  if (typeof value !== "string" || !value) return undefined;
  if (value.startsWith("{")) {
    try {
      const parsed = JSON.parse(value);
      if (parsed?.v === 1 && typeof parsed.id === "string") {
        return {
          id: parsed.id,
          ...(["commentary", "final_answer"].includes(parsed.phase)
            ? { phase: parsed.phase }
            : {}),
        };
      }
    } catch {
      // A legacy signature may itself start with a brace.
    }
  }
  return { id: value };
}

function textSignature(id, phase) {
  const value = { v: 1, id };
  if (["commentary", "final_answer"].includes(phase)) value.phase = phase;
  return JSON.stringify(value);
}

function toolCallIds(value) {
  if (typeof value !== "string" || !value) {
    throw new OpenAIUpstreamError("Pi tool call has no identity");
  }
  const separator = value.indexOf("|");
  if (separator > 0 && separator < value.length - 1) {
    return { callId: value.slice(0, separator), itemId: value.slice(separator + 1) };
  }
  return { callId: value, itemId: undefined };
}

function contentIndex(event) {
  if (!Number.isSafeInteger(event.contentIndex) || event.contentIndex < 0) {
    throw new OpenAIUpstreamError("Pi event has an invalid content index");
  }
  return event.contentIndex;
}

function providerFailureMessage(error) {
  if (error instanceof OpenAIUpstreamError) return error.message;
  return "Cyclo Provider stream failed";
}

function safeErrorMessage(value) {
  const clean = String(value ?? "Cyclo Provider inference failed")
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, MAX_ERROR_MESSAGE_LENGTH);
  return clean || "Cyclo Provider inference failed";
}

function requestError(message, param = null, code = "invalid_request_error") {
  return new OpenAIRequestError(message, { param, code });
}

function rejectExtraFields(value, allowed, param) {
  const accepted = new Set(allowed);
  if (Object.keys(value).some((field) => !accepted.has(field))) {
    throw requestError(`${param} contains an unsupported parameter`, param,
      "unsupported_parameter");
  }
}

function requiredString(value, param, empty = false) {
  if (typeof value !== "string" || (!empty && !value)) {
    throw requestError(`${param} must be ${empty ? "a" : "a non-empty"} string`, param);
  }
  return value;
}

function safeValue(value) {
  return String(value ?? "unknown")
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, 128) || "unknown";
}

function callOptions(signal, timeoutMs) {
  const options = {};
  if (signal !== undefined) options.signal = signal;
  if (timeoutMs !== undefined) options.timeoutMs = timeoutMs;
  return options;
}

function defaultIdFactory(prefix) {
  return `${prefix}_${randomUUID().replaceAll("-", "")}`;
}

function clone(value) {
  return structuredClone(value);
}

function plainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
