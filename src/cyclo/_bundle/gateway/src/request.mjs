import { toJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import {
  MessageRole,
  Modality,
  ToolChoiceMode,
} from "@cyclo/provider/contract";

const MAX_SAFE_UINT64 = BigInt(Number.MAX_SAFE_INTEGER);
const TOOL_NAME = /^[A-Za-z0-9_-]{1,64}$/u;
const CALL_ID = /^[A-Za-z0-9_-]{1,64}$/u;
const IMAGE_MEDIA_TYPES = new Set([
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
]);
const SCHEMA_TYPES = new Set(["array", "boolean", "integer", "number", "object", "string"]);
const SCHEMA_KEYS = new Set([
  "additionalProperties",
  "description",
  "enum",
  "items",
  "maximum",
  "maxItems",
  "maxLength",
  "minimum",
  "minItems",
  "minLength",
  "properties",
  "required",
  "type",
]);

// Validate the complete portable request before the adapter opens an upstream
// stream. pi-ai is then only a native protocol adapter, never a validator for
// the Cyclo boundary.
export function prepareInference(request, route) {
  if (!route || request.model !== route.publicModel.id) {
    invalid("model does not match the selected route");
  }
  if ((request.extensions ?? []).length !== 0) {
    invalid("this gateway does not accept request extensions");
  }

  const capabilities = route.publicModel.capabilities;
  const tools = prepareTools(request.tools ?? [], capabilities);
  const messages = prepareMessages(request.input ?? [], route, tools);
  if (messages.length === 0) invalid("input must contain at least one message");

  const generation = prepareGeneration(request.generation, route, tools);
  return {
    context: {
      ...(request.instructions ? { systemPrompt: request.instructions } : {}),
      messages,
      ...(generation.exposeTools && tools.length ? { tools } : {}),
    },
    generation,
  };
}

function prepareTools(input, capabilities) {
  if (input.length && !capabilities.functionTools) {
    invalid("the selected model does not support function tools");
  }
  const names = new Set();
  return input.map((tool) => {
    if (!TOOL_NAME.test(tool.name)) {
      invalid("tool names must contain 1-64 ASCII letters, digits, underscores, or hyphens");
    }
    const normalizedName = tool.name.toLowerCase();
    if (names.has(normalizedName)) invalid(`duplicate tool name ${tool.name}`);
    names.add(normalizedName);
    const parameters = structJson(tool.inputSchema, `tool ${tool.name} input_schema`);
    validateToolSchema(parameters, `tool ${tool.name} input_schema`);
    return {
      name: tool.name,
      description: typeof tool.description === "string" ? tool.description : "",
      parameters,
    };
  });
}

function prepareMessages(input, route, tools) {
  const messages = [];
  const calls = new Map();
  const unresolved = new Set();
  const toolNames = new Set(tools.map(({ name }) => name));

  for (const [index, item] of input.entries()) {
    if ((item.extensions ?? []).length !== 0) {
      invalid(`input item ${index} has unsupported extensions`);
    }
    const value = item.item;
    if (!value || !value.case) invalid(`input item ${index} has no value`);

    if (value.case === "message") {
      if (unresolved.size) invalid("a message follows a tool call without its result");
      const message = value.value;
      const content = contentParts(message.content ?? [], route, message.role, `message ${index}`);
      if (content.length === 0) invalid(`message ${index} has no content`);
      if (message.role === MessageRole.USER) {
        messages.push({
          role: "user",
          content: collapseText(content),
          timestamp: 0,
        });
      } else if (message.role === MessageRole.ASSISTANT) {
        if (content.some(({ type }) => type !== "text")) {
          invalid("assistant messages may contain only text");
        }
        messages.push(assistantMessage(route, content));
      } else {
        invalid(`message ${index} has an invalid role`);
      }
      continue;
    }

    if (value.case === "toolCall") {
      const call = value.value;
      if (!CALL_ID.test(call.id) || !TOOL_NAME.test(call.name)) {
        invalid(`tool call ${index} has an invalid id or name`);
      }
      if (calls.has(call.id)) invalid(`duplicate tool call id ${call.id}`);
      if (!toolNames.has(call.name)) invalid(`tool call ${call.id} names an undeclared tool`);
      const block = {
        type: "toolCall",
        id: call.id,
        name: call.name,
        arguments: structJson(call.arguments, `tool call ${call.id} arguments`),
      };
      const previous = messages.at(-1);
      if (previous?.role === "assistant" && previous.stopReason !== "toolUse") {
        previous.content.push(block);
        previous.stopReason = "toolUse";
      } else if (previous?.role === "assistant" && previous.stopReason === "toolUse") {
        previous.content.push(block);
      } else {
        messages.push(assistantMessage(route, [block], "toolUse"));
      }
      calls.set(call.id, call.name);
      unresolved.add(call.id);
      continue;
    }

    if (value.case === "toolResult") {
      const result = value.value;
      const name = calls.get(result.callId);
      if (!name || !unresolved.has(result.callId)) {
        invalid(`tool result ${index} does not match an unresolved earlier call`);
      }
      if (result.isError === true && route.rawModel.api.includes("responses")) {
        invalid("error tool results are unsupported by Responses API models");
      }
      messages.push({
        role: "toolResult",
        toolCallId: result.callId,
        toolName: name,
        content: contentParts(result.content ?? [], route, MessageRole.USER, `tool result ${index}`),
        isError: result.isError === true,
        timestamp: 0,
      });
      unresolved.delete(result.callId);
      continue;
    }

    if (value.case === "reasoningSummary") {
      invalid("this gateway does not accept reasoning history yet");
    }
    invalid(`input item ${index} has an unsupported kind`);
  }

  if (unresolved.size) invalid("input ends with a tool call without its result");
  return messages;
}

function contentParts(parts, route, role, label) {
  const result = [];
  for (const [index, part] of parts.entries()) {
    const content = part.content;
    if (content?.case === "text") {
      if (typeof content.value !== "string" || content.value.length === 0) {
        invalid(`${label} content ${index} has empty text`);
      }
      result.push({ type: "text", text: content.value });
      continue;
    }
    if (content?.case === "media") {
      if (role !== MessageRole.USER) invalid(`${label} may not contain media`);
      if (!route.publicModel.capabilities.inputModalities.includes(Modality.IMAGE)) {
        invalid("the selected model does not support image input");
      }
      const media = content.value;
      if (!IMAGE_MEDIA_TYPES.has(media.mediaType) || !hasImageSignature(media.mediaType, media.data)) {
        invalid(`${label} content ${index} is not a valid inline image`);
      }
      result.push({
        type: "image",
        data: Buffer.from(media.data).toString("base64"),
        mimeType: media.mediaType,
      });
      continue;
    }
    invalid(`${label} content ${index} has no value`);
  }
  return result;
}

function prepareGeneration(value, route, tools) {
  const generation = value ?? {};
  let maxTokens;
  if (generation.maxOutputTokens !== undefined) {
    const requested = generation.maxOutputTokens;
    if (requested <= 0n || requested > MAX_SAFE_UINT64) {
      invalid("max_output_tokens is outside the supported range");
    }
    if (
      route.publicModel.maxOutputTokens !== undefined
      && requested > route.publicModel.maxOutputTokens
    ) {
      invalid("max_output_tokens exceeds the selected model limit");
    }
    maxTokens = Number(requested);
    if (route.rawModel.api.includes("responses") && maxTokens < 16) {
      invalid("Responses API models require at least 16 max_output_tokens");
    }
  }

  let temperature;
  if (generation.temperature !== undefined) {
    if (!route.publicModel.capabilities.temperature) {
      invalid("the selected model does not support temperature");
    }
    if (!finiteUnit(generation.temperature)) invalid("temperature must be between 0 and 1");
    temperature = generation.temperature;
  }
  if (generation.topP !== undefined) invalid("top_p is unsupported by this gateway");
  if ((generation.stopSequences ?? []).length !== 0) {
    invalid("stop sequences are unsupported by this gateway");
  }

  const choice = generation.toolChoice;
  const mode = choice?.mode ?? ToolChoiceMode.AUTO;
  if (mode === ToolChoiceMode.UNSPECIFIED) invalid("tool_choice mode is unspecified");
  if (mode !== ToolChoiceMode.AUTO && tools.length === 0) {
    invalid("tool_choice requires at least one declared tool");
  }
  if (mode === ToolChoiceMode.SPECIFIC) {
    if (!tools.some(({ name }) => name === choice.toolName)) {
      invalid("tool_choice names an undeclared tool");
    }
  } else if (choice?.toolName) {
    invalid("tool_name is valid only for a specific tool choice");
  }
  if (![ToolChoiceMode.AUTO, ToolChoiceMode.NONE, ToolChoiceMode.REQUIRED, ToolChoiceMode.SPECIFIC].includes(mode)) {
    invalid("tool_choice mode is unsupported");
  }

  return {
    maxTokens,
    temperature,
    toolChoice: { mode, toolName: choice?.toolName ?? "" },
    exposeTools: mode !== ToolChoiceMode.NONE,
  };
}

function assistantMessage(route, content, stopReason = "stop") {
  return {
    role: "assistant",
    content: [...content],
    api: route.rawModel.api,
    provider: route.rawModel.provider,
    model: route.rawModel.id,
    usage: zeroUsage(),
    stopReason,
    timestamp: 0,
  };
}

function collapseText(content) {
  return content.length === 1 && content[0].type === "text" ? content[0].text : content;
}

function structJson(value, label) {
  if (value === undefined) return {};
  let result;
  try {
    result = toJson(StructSchema, value);
  } catch {
    invalid(`${label} is not a valid protobuf Struct`);
  }
  if (!isObject(result)) invalid(`${label} must be an object`);
  return result;
}

// This intentionally small JSON Schema dialect is carried unchanged by every
// native adapter exposed by the gateway. Anything outside it is rejected here
// instead of being dropped by an adapter or rejected after dispatch.
function validateToolSchema(root, label) {
  let nodes = 0;

  function visit(schema, path, depth) {
    if (!isObject(schema)) invalid(`${path} must be an object`);
    if (depth > 16 || ++nodes > 256) invalid(`${label} is too complex`);
    for (const key of Object.keys(schema)) {
      if (!SCHEMA_KEYS.has(key)) invalid(`${path} uses unsupported keyword ${key}`);
    }
    if (!SCHEMA_TYPES.has(schema.type)) invalid(`${path} has an unsupported type`);
    if (schema.description !== undefined && typeof schema.description !== "string") {
      invalid(`${path} has an invalid description`);
    }

    if (schema.enum !== undefined) {
      if (
        !Array.isArray(schema.enum)
        || schema.enum.length === 0
        || schema.enum.some((value) => !matchesSchemaType(value, schema.type))
        || new Set(schema.enum.map((value) => JSON.stringify(value))).size !== schema.enum.length
      ) {
        invalid(`${path} has an invalid enum`);
      }
    }

    if (schema.type === "object") {
      if (schema.properties !== undefined && !isObject(schema.properties)) {
        invalid(`${path} has invalid properties`);
      }
      const properties = schema.properties ?? {};
      for (const [name, child] of Object.entries(properties)) {
        if (!validPropertyName(name)) invalid(`${path} has an invalid property name`);
        visit(child, `${path}.properties.${name}`, depth + 1);
      }
      if (
        schema.required !== undefined
        && (!Array.isArray(schema.required)
          || schema.required.some((name) => typeof name !== "string" || !Object.hasOwn(properties, name))
          || new Set(schema.required).size !== schema.required.length)
      ) {
        invalid(`${path} has an invalid required list`);
      }
      if (
        schema.additionalProperties !== undefined
        && typeof schema.additionalProperties !== "boolean"
      ) {
        invalid(`${path} has invalid additionalProperties`);
      }
      rejectPresent(schema, path, ["items", "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength"]);
      return;
    }

    rejectPresent(schema, path, ["properties", "required", "additionalProperties"]);
    if (schema.type === "array") {
      if (schema.items === undefined) invalid(`${path} requires items`);
      visit(schema.items, `${path}.items`, depth + 1);
      validateBounds(schema, path, "minItems", "maxItems", true);
      rejectPresent(schema, path, ["minimum", "maximum", "minLength", "maxLength"]);
      return;
    }

    rejectPresent(schema, path, ["items", "minItems", "maxItems"]);
    if (schema.type === "string") {
      validateBounds(schema, path, "minLength", "maxLength", true);
      rejectPresent(schema, path, ["minimum", "maximum"]);
    } else if (schema.type === "number" || schema.type === "integer") {
      validateBounds(schema, path, "minimum", "maximum", false);
      rejectPresent(schema, path, ["minLength", "maxLength"]);
    } else {
      rejectPresent(schema, path, ["minimum", "maximum", "minLength", "maxLength"]);
    }
  }

  visit(root, label, 0);
  if (root.type !== "object") invalid(`${label} must describe an object`);
}

function validateBounds(schema, path, minimum, maximum, integer) {
  for (const name of [minimum, maximum]) {
    if (
      schema[name] !== undefined
      && (typeof schema[name] !== "number"
        || !Number.isFinite(schema[name])
        || (integer && (!Number.isInteger(schema[name]) || schema[name] < 0)))
    ) {
      invalid(`${path} has an invalid ${name}`);
    }
  }
  if (schema[minimum] !== undefined && schema[maximum] !== undefined && schema[minimum] > schema[maximum]) {
    invalid(`${path} has inverted ${minimum}/${maximum}`);
  }
}

function rejectPresent(schema, path, names) {
  const name = names.find((candidate) => schema[candidate] !== undefined);
  if (name) invalid(`${path} cannot use ${name} with type ${schema.type}`);
}

function matchesSchemaType(value, type) {
  if (type === "string") return typeof value === "string";
  if (type === "boolean") return typeof value === "boolean";
  if (type === "integer") return Number.isSafeInteger(value);
  if (type === "number") return typeof value === "number" && Number.isFinite(value);
  return false;
}

function validPropertyName(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 128
    && !/[\u0000-\u001f\u007f]/u.test(value);
}

function hasImageSignature(mediaType, data) {
  if (!(data instanceof Uint8Array)) return false;
  const startsWith = (...bytes) => bytes.every((byte, index) => data[index] === byte);
  if (mediaType === "image/png") {
    return startsWith(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a);
  }
  if (mediaType === "image/jpeg") return startsWith(0xff, 0xd8, 0xff);
  if (mediaType === "image/gif") {
    return startsWith(0x47, 0x49, 0x46, 0x38, 0x37, 0x61)
      || startsWith(0x47, 0x49, 0x46, 0x38, 0x39, 0x61);
  }
  return startsWith(0x52, 0x49, 0x46, 0x46)
    && data[8] === 0x57
    && data[9] === 0x45
    && data[10] === 0x42
    && data[11] === 0x50;
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

function finiteUnit(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function invalid(message) {
  throw new ConnectError(message, Code.InvalidArgument);
}
