import { fromJson, toJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import {
  FinishReason,
  MessageRole,
  Modality,
} from "@cyclo/provider/contract";
import { validateInferStream } from "@cyclo/provider/protocol";

const API = "cyclo-provider-v1";
const PROVIDER = /^[a-z0-9_-]+$/u;
const MODEL = /^[^\s\u0000-\u001f\u007f]+$/u;
const RESERVED_PROVIDERS = new Set(["__proto__", "constructor", "gateway", "prototype"]);
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u;
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
    const { model, capabilities } = piModel(portable, id);
    group.push({ publicId: portable.id, model, capabilities });
    groups.set(provider, group);
  }
  return groups;
}

export function streamProvider(client, publicId, model, context, options = {}) {
  const stream = createAssistantMessageEventStream();
  const partial = assistantMessage(model);
  void pump(stream, partial, client, publicId, model, context, options);
  return stream;
}

async function pump(stream, partial, client, publicId, model, context, options) {
  try {
    const request = inferRequest(publicId, model, context, options);
    const responses = client.infer(request, callOptions(options));
    await consume(stream, partial, validateInferStream(responses, { model: publicId }), model);
  } catch (error) {
    const aborted = options.signal?.aborted || (error instanceof ConnectError && error.code === Code.Canceled);
    partial.stopReason = aborted ? "aborted" : "error";
    partial.errorMessage = aborted
      ? "Cyclo provider request aborted"
      : `Cyclo provider request failed: ${providerErrorMessage(error)}`;
    stream.push({
      type: "error",
      reason: partial.stopReason,
      error: partial,
    });
  }
}

function providerErrorMessage(error) {
  const raw = error instanceof Error ? error.message : String(error ?? "unknown error");
  const safe = raw.replace(/[\u0000-\u001f\u007f]/gu, " ").replace(/\s+/gu, " ").trim();
  return safe.slice(0, 512) || "unknown provider error";
}

async function consume(stream, partial, responses, model) {
  const open = new Map();

  for await (const response of responses) {
    const { case: kind, value } = response.event;
    if (kind === "started") {
      noExtensions(value.extensions, "Started");
      partial.responseId = value.responseId;
      partial.responseModel = value.model;
      stream.push({ type: "start", partial });
      continue;
    }

    if (kind === "itemStarted") {
      const contentIndex = partial.content.length;
      const item = outputItem(value.item, model);
      partial.content.push(item.content);
      open.set(value.index, { ...item, contentIndex });
      stream.push({ type: item.start, contentIndex, partial });
      continue;
    }

    if (kind === "itemDelta") {
      const item = open.get(value.index);
      if (!item) throw new TypeError("Provider delta names an unknown item");
      const delta = outputDelta(item, value.delta);
      stream.push({
        type: item.delta,
        contentIndex: item.contentIndex,
        delta,
        partial,
      });
      continue;
    }

    if (kind === "itemFinished") {
      noExtensions(value.extensions, `item ${value.index}`);
      const item = open.get(value.index);
      if (!item) throw new TypeError("Provider finished an unknown item");
      const event = outputFinished(item, value);
      open.delete(value.index);
      stream.push({ ...event, contentIndex: item.contentIndex, partial });
      continue;
    }

    if (kind === "finished") {
      noExtensions(value.extensions, "Finished");
      partial.usage = piUsage(value.usage);
      partial.stopReason = stopReason(value.reason);
      stream.push({
        type: "done",
        reason: partial.stopReason,
        message: partial,
      });
      continue;
    }

    throw new TypeError("Provider emitted an unsupported event");
  }
}

function outputItem(item, model) {
  switch (item.case) {
    case "text":
      requireCapability(model.capabilities.outputModalities.includes(Modality.TEXT));
      return {
        content: { type: "text", text: "" },
        kind: "text",
        start: "text_start",
        delta: "text_delta",
      };
    case "reasoningSummary":
      requireCapability(model.capabilities.reasoningSummaries);
      return {
        content: { type: "thinking", thinking: "" },
        kind: "reasoning",
        start: "thinking_start",
        delta: "thinking_delta",
      };
    case "toolCall":
      requireCapability(model.capabilities.functionTools);
      return {
        content: {
          type: "toolCall",
          id: item.value.id,
          name: item.value.name,
          arguments: {},
        },
        kind: "tool",
        start: "toolcall_start",
        delta: "toolcall_delta",
      };
    default:
      throw new TypeError(`Pi cannot represent Provider output item ${item.case}`);
  }
}

function outputDelta(item, delta) {
  if (item.kind === "text" && delta.case === "text") {
    item.content.text += delta.value;
    return delta.value;
  }
  if (item.kind === "reasoning" && delta.case === "text") {
    item.content.thinking += delta.value;
    return delta.value;
  }
  if (item.kind === "tool" && delta.case === "toolArgumentsJson") {
    return delta.value;
  }
  throw new TypeError("Provider emitted an incompatible output delta");
}

function outputFinished(item, value) {
  if (item.kind === "text") {
    return { type: "text_end", content: item.content.text };
  }
  if (item.kind === "reasoning") {
    return { type: "thinking_end", content: item.content.thinking };
  }
  const arguments_ = structJson(value.toolArguments, "tool arguments");
  item.content.arguments = arguments_;
  return { type: "toolcall_end", toolCall: item.content };
}

function inferRequest(publicId, model, context, options) {
  if (!context || !Array.isArray(context.messages)) {
    throw new TypeError("Pi context has no message list");
  }
  if (options.reasoning !== undefined && options.reasoning !== "off") {
    throw new TypeError("Cyclo Provider has no portable reasoning-effort control");
  }
  if (context.systemPrompt !== undefined && typeof context.systemPrompt !== "string") {
    throw new TypeError("Pi system prompt must be a string");
  }
  const tools = portableTools(context.tools ?? [], model);
  return {
    model: publicId,
    instructions: typeof context.systemPrompt === "string" ? context.systemPrompt : "",
    input: context.messages.flatMap((message) => portableMessage(message, model)),
    tools,
    generation: generation(options, model),
  };
}

function portableMessage(message, model) {
  if (!message || typeof message !== "object") throw new TypeError("Pi message is invalid");
  switch (message.role) {
    case "user":
      return [messageItem(MessageRole.USER, portableContent(message.content, model, "user"))];
    case "assistant": {
      if (!Array.isArray(message.content) || message.content.length === 0) {
        throw new TypeError("Pi assistant message has no content");
      }
      return message.content.flatMap((content) => assistantItem(content, model));
    }
    case "toolResult":
      if (typeof message.isError !== "boolean") {
        throw new TypeError("Pi tool result isError must be boolean");
      }
      return [{ item: { case: "toolResult", value: {
        callId: requiredString(message.toolCallId, "tool result call id"),
        content: portableContent(message.content, model, "tool result"),
        isError: message.isError === true,
      } } }];
    default:
      throw new TypeError(`unsupported Pi message role ${message.role}`);
  }
}

function assistantItem(content, model) {
  switch (content?.type) {
    case "text":
      if (content.textSignature !== undefined) {
        throw new TypeError("signed Pi text has no portable representation");
      }
      return [messageItem(MessageRole.ASSISTANT, [textPart(content.text)])];
    case "thinking":
      if (!model.capabilities.reasoningSummaries) {
        throw new TypeError("selected model does not accept reasoning summaries");
      }
      if (content.thinkingSignature !== undefined || content.redacted === true) {
        throw new TypeError("signed Pi thinking has no portable representation");
      }
      return [{ item: { case: "reasoningSummary", value: {
        text: requiredString(content.thinking, "reasoning summary"),
      } } }];
    case "toolCall":
      if (!model.capabilities.functionTools) {
        throw new TypeError("selected model does not accept function tools");
      }
      if (content.thoughtSignature !== undefined) {
        throw new TypeError("signed Pi tool calls have no portable representation");
      }
      return [{ item: { case: "toolCall", value: {
        id: requiredString(content.id, "tool call id"),
        name: requiredString(content.name, "tool call name"),
        arguments: jsonStruct(content.arguments, "tool call arguments"),
      } } }];
    default:
      throw new TypeError(`unsupported Pi assistant content ${content?.type}`);
  }
}

function portableContent(content, model, label) {
  const values = typeof content === "string" ? [{ type: "text", text: content }] : content;
  if (!Array.isArray(values) || values.length === 0) {
    throw new TypeError(`${label} content is empty`);
  }
  return values.map((part) => {
    if (part?.type === "text") {
      if (part.textSignature !== undefined) {
        throw new TypeError("signed Pi text has no portable representation");
      }
      return textPart(part.text);
    }
    if (part?.type === "image") {
      if (!model.capabilities.inputModalities.includes(Modality.IMAGE)) {
        throw new TypeError("selected model does not accept image input");
      }
      return { content: { case: "media", value: {
        mediaType: requiredString(part.mimeType, "image media type"),
        data: base64(part.data),
      } } };
    }
    throw new TypeError(`unsupported ${label} content ${part?.type}`);
  });
}

function portableTools(tools, model) {
  if (!Array.isArray(tools)) throw new TypeError("Pi tools must be an array");
  if (tools.length && !model.capabilities.functionTools) {
    throw new TypeError("selected model does not accept function tools");
  }
  return tools.map((tool) => ({
    name: requiredString(tool?.name, "tool name"),
    description: optionalString(tool.description, "tool description"),
    inputSchema: jsonStruct(tool.parameters, `tool ${tool?.name} schema`),
  }));
}

function generation(options, model) {
  const result = {};
  if (options.maxTokens !== undefined) {
    const value = positiveSafeInteger(options.maxTokens, "maxTokens");
    if (value > model.maxTokens) throw new TypeError("maxTokens exceeds the model limit");
    result.maxOutputTokens = BigInt(value);
  }
  if (options.temperature !== undefined) {
    if (!model.capabilities.temperature) {
      throw new TypeError("selected model does not accept temperature");
    }
    if (!Number.isFinite(options.temperature) || options.temperature < 0 || options.temperature > 1) {
      throw new TypeError("temperature must be between 0 and 1");
    }
    result.temperature = options.temperature;
  }
  return result;
}

function piModel(portable, id) {
  const capabilities = portable?.capabilities;
  if (!capabilities || !Array.isArray(capabilities.inputModalities)
      || !Array.isArray(capabilities.outputModalities)) {
    throw new TypeError(`model ${portable?.id} has invalid capabilities`);
  }
  if ((portable.extensions ?? []).length || (capabilities.extensionTypes ?? []).length) {
    throw new TypeError(`model ${portable.id} requires unsupported typed extensions`);
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
  const contextWindow = uint64Number(portable.contextWindowTokens, "context window", portable.id);
  const maxTokens = uint64Number(portable.maxOutputTokens, "output limit", portable.id);
  if (portable.displayName !== undefined && typeof portable.displayName !== "string") {
    throw new TypeError(`model ${portable.id} has an invalid display name`);
  }
  return Object.freeze({
    model: Object.freeze({
      id,
      name: typeof portable.displayName === "string" && portable.displayName
        ? portable.displayName
        : id,
      reasoning: false,
      input: Object.freeze(capabilities.inputModalities.includes(Modality.IMAGE)
        ? ["text", "image"]
        : ["text"]),
      cost: ZERO_COST,
      contextWindow,
      maxTokens,
    }),
    capabilities: Object.freeze(structuredClone(capabilities)),
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

function messageItem(role, content) {
  return { item: { case: "message", value: { role, content } } };
}

function textPart(value) {
  return { content: { case: "text", value: requiredString(value, "text content") } };
}

function jsonStruct(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  try {
    return fromJson(StructSchema, value);
  } catch {
    throw new TypeError(`${label} is not JSON-compatible`);
  }
}

function structJson(value, label) {
  let result;
  try {
    result = toJson(StructSchema, value);
  } catch {
    throw new TypeError(`${label} is not a valid object`);
  }
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new TypeError(`${label} is not an object`);
  }
  return result;
}

function base64(value) {
  if (typeof value !== "string" || !value || !BASE64.test(value)) {
    throw new TypeError("image data is not canonical base64");
  }
  const decoded = Buffer.from(value, "base64");
  if (decoded.length === 0) throw new TypeError("image data is empty");
  return decoded;
}

function uint64Number(value, label, model) {
  if (typeof value !== "bigint" || value <= 0n || value > MAX_SAFE_UINT64) {
    throw new TypeError(`model ${model} has no usable ${label}`);
  }
  return Number(value);
}

function positiveSafeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${label} must be positive`);
  return value;
}

function requiredString(value, label) {
  if (typeof value !== "string" || value.length === 0) throw new TypeError(`${label} is empty`);
  return value;
}

function optionalString(value, label) {
  if (value === undefined) return "";
  if (typeof value !== "string") throw new TypeError(`${label} must be a string`);
  return value;
}

function noExtensions(values, label) {
  if ((values ?? []).length) throw new TypeError(`${label} extensions cannot be represented by Pi`);
}

function requireCapability(value) {
  if (!value) throw new TypeError("Provider emitted an output the model did not advertise");
}

function piUsage(value) {
  if (value === undefined) return zeroUsage();
  const input = usageNumber(value.inputTokens, "input tokens");
  const output = usageNumber(value.outputTokens, "output tokens");
  if (input === undefined || output === undefined) {
    throw new TypeError("Provider usage is missing input or output tokens");
  }
  const cached = usageNumber(value.cachedInputTokens, "cached input tokens", 0);
  const reasoning = usageNumber(value.reasoningTokens, "reasoning tokens", undefined);
  return {
    input: input - cached,
    output,
    cacheRead: cached,
    cacheWrite: 0,
    ...(reasoning === undefined ? {} : { reasoning }),
    totalTokens: input + output,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

function callOptions(options) {
  const result = {};
  if (options.signal !== undefined) result.signal = options.signal;
  if (options.timeoutMs !== undefined) {
    result.timeoutMs = positiveSafeInteger(options.timeoutMs, "timeoutMs");
  }
  return result;
}

function usageNumber(value, label, absent) {
  if (value === undefined) return absent;
  if (typeof value !== "bigint" || value < 0n || value > MAX_SAFE_UINT64) {
    throw new TypeError(`Provider ${label} are invalid`);
  }
  return Number(value);
}

function zeroUsage() {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

function assistantMessage(model) {
  return {
    role: "assistant",
    content: [],
    api: API,
    provider: model.provider,
    model: model.id,
    usage: zeroUsage(),
    stopReason: "stop",
    timestamp: Date.now(),
  };
}

function stopReason(value) {
  switch (value) {
    case FinishReason.STOP:
    case FinishReason.STOP_SEQUENCE:
      return "stop";
    case FinishReason.MAX_OUTPUT_TOKENS:
      return "length";
    case FinishReason.TOOL_CALLS:
      return "toolUse";
    default:
      throw new TypeError("Pi cannot represent the Provider finish reason");
  }
}
