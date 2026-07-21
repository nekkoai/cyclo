import { randomUUID } from "node:crypto";

import { fromJson } from "@bufbuild/protobuf";
import { StructSchema } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";
import { stream as streamAnthropic } from "@earendil-works/pi-ai/api/anthropic-messages";
import { stream as streamCodex } from "@earendil-works/pi-ai/api/openai-codex-responses";
import { stream as streamOpenAI } from "@earendil-works/pi-ai/api/openai-responses";
import {
  FinishReason,
  ToolChoiceMode,
} from "@cyclo/provider/contract";

import { createTextRedactor, normalizeSecrets, redactValue } from "./secrets.mjs";

const DEFAULT_STREAMERS = Object.freeze({
  "anthropic-messages": streamAnthropic,
  "openai-codex-responses": streamCodex,
  "openai-responses": streamOpenAI,
});
const TOOL_NAME = /^[A-Za-z0-9_-]{1,64}$/u;
const CALL_ID = /^[A-Za-z0-9_-]{1,64}$/u;

export function createPiAdapter({ streamers = DEFAULT_STREAMERS } = {}) {
  return Object.freeze({
    infer(route, prepared, credential, signal) {
      return translate(route, prepared, credential, signal, streamers);
    },
  });
}

async function* translate(route, prepared, credential, signal, streamers) {
  const stream = streamers[route.rawModel.api];
  if (typeof stream !== "function") {
    throw new ConnectError("model adapter is unavailable", Code.Unavailable);
  }
  const secrets = normalizeSecrets(credential.sensitiveValues);
  const dispatch = new AbortController();
  const dispatchSignal = signal
    ? AbortSignal.any([signal, dispatch.signal])
    : dispatch.signal;
  const nativeToolNames = new Map(
    (prepared.context.tools ?? []).map((tool) => [tool.name, tool.name]),
  );
  const options = nativeOptions(
    route,
    prepared,
    credential.apiKey,
    dispatchSignal,
    nativeToolNames,
  );

  let native;
  try {
    native = stream(route.rawModel, prepared.context, options);
  } catch (error) {
    dispatch.abort(new Error("gateway native dispatch stopped"));
    throw sanitizedFailure(error, signal);
  }

  let started = false;
  let terminal = false;
  let nextIndex = 0;
  const textItems = new Map();
  const toolItems = new Map();
  const toolIds = new Set();

  try {
    for await (const event of native) {
      if (terminal) throw malformed("event follows terminal event");
      switch (event?.type) {
        case "start":
          if (started) throw malformed("duplicate native start");
          started = true;
          yield {
            event: {
              case: "started",
              value: { responseId: randomUUID(), model: route.publicModel.id },
            },
          };
          break;

        case "text_start": {
          requireStarted(started);
          if (textItems.has(event.contentIndex)) throw malformed("duplicate text item");
          const index = nextIndex++;
          textItems.set(event.contentIndex, {
            index,
            redactor: createTextRedactor(secrets),
            nativeText: "",
          });
          yield {
            event: {
              case: "itemStarted",
              value: { index, item: { case: "text", value: {} } },
            },
          };
          break;
        }

        case "text_delta": {
          const item = textItems.get(event.contentIndex);
          if (!item) throw malformed("text delta has no open item");
          if (typeof event.delta !== "string") throw malformed("native text delta is invalid");
          item.nativeText += event.delta;
          const text = item.redactor.push(event.delta);
          if (text) yield textDelta(item.index, text);
          break;
        }

        case "text_end": {
          const item = textItems.get(event.contentIndex);
          if (!item) throw malformed("text end has no open item");
          if (typeof event.content !== "string" || !event.content.startsWith(item.nativeText)) {
            throw malformed("native final text disagrees with its deltas");
          }
          const missing = event.content.slice(item.nativeText.length);
          if (missing) {
            const text = item.redactor.push(missing);
            if (text) yield textDelta(item.index, text);
          }
          const tail = item.redactor.flush();
          if (tail) yield textDelta(item.index, tail);
          yield {
            event: { case: "itemFinished", value: { index: item.index } },
          };
          textItems.delete(event.contentIndex);
          break;
        }

        // Reasoning is intentionally neither advertised nor exposed. Native
        // adapters may still use it internally for models that always reason.
        case "thinking_start":
        case "thinking_delta":
        case "thinking_end":
          requireStarted(started);
          break;

        case "toolcall_start": {
          requireStarted(started);
          if (toolItems.has(event.contentIndex)) throw malformed("duplicate native tool item");
          const call = event.partial?.content?.[event.contentIndex];
          if (call?.type !== "toolCall" || typeof call.id !== "string" || typeof call.name !== "string") {
            throw malformed("native tool call start is incomplete");
          }
          const id = publicToolId(route.rawModel.api, call.id);
          const name = nativeToolNames.get(call.name);
          validateToolIdentity(id, name, prepared, secrets, toolIds);
          const index = nextIndex++;
          toolIds.add(id);
          toolItems.set(event.contentIndex, {
            index,
            id,
            name,
            nativeName: call.name,
          });
          yield {
            event: {
              case: "itemStarted",
              value: {
                index,
                item: { case: "toolCall", value: { id, name } },
              },
            },
          };
          break;
        }

        case "toolcall_delta":
          requireStarted(started);
          if (!toolItems.has(event.contentIndex) || typeof event.delta !== "string") {
            throw malformed("native tool delta has no open item");
          }
          // The final parsed object is authoritative. Avoid relaying partial
          // JSON until it can be validated and scrubbed as a complete value.
          break;

        case "toolcall_end": {
          requireStarted(started);
          const item = toolItems.get(event.contentIndex);
          if (!item) throw malformed("native tool end has no open item");
          const call = event.toolCall;
          if (!call || typeof call.id !== "string" || typeof call.name !== "string") {
            throw malformed("native tool call is incomplete");
          }
          const id = publicToolId(route.rawModel.api, call.id);
          if (id !== item.id || call.name !== item.nativeName) {
            throw malformed("native tool call changed identity while streaming");
          }
          const safeArguments = redactValue(call.arguments ?? {}, secrets);
          if (!plainObject(safeArguments)) throw malformed("native tool arguments are not an object");
          let toolArguments;
          try {
            toolArguments = fromJson(StructSchema, safeArguments);
          } catch {
            throw malformed("native tool arguments are not JSON-compatible");
          }
          yield {
            event: {
              case: "itemFinished",
              value: { index: item.index, toolArguments },
            },
          };
          toolItems.delete(event.contentIndex);
          break;
        }

        case "done": {
          requireStarted(started);
          if (textItems.size || toolItems.size) {
            throw malformed("native stream finished with open output items");
          }
          terminal = true;
          yield {
            event: {
              case: "finished",
              value: {
                reason: finishReason(event.reason),
                usage: portableUsage(event.message?.usage),
              },
            },
          };
          break;
        }

        case "error":
          throw sanitizedFailure(event.error, signal);

        default:
          throw malformed("native stream emitted an unknown event");
      }
    }
  } catch (error) {
    if (error instanceof ConnectError) throw error;
    throw sanitizedFailure(error, signal);
  } finally {
    dispatch.abort(new Error("gateway native dispatch stopped"));
  }

  if (!terminal) throw malformed("native stream ended before a terminal event");
}

function nativeOptions(route, prepared, apiKey, signal, nativeToolNames) {
  const { generation } = prepared;
  const options = {
    apiKey,
    signal,
    maxRetries: 0,
    ...(generation.maxTokens === undefined ? {} : { maxTokens: generation.maxTokens }),
    ...(generation.temperature === undefined ? {} : { temperature: generation.temperature }),
  };

  const nativeChoice = toolChoice(route.rawModel.api, generation.toolChoice);
  if (route.rawModel.api === "anthropic-messages") {
    if (nativeChoice !== undefined) options.toolChoice = nativeChoice;
    if (prepared.context.tools?.length) {
      options.onPayload = (payload) => canonicalizeAnthropicPayload(
        payload,
        prepared.context.tools,
        generation.toolChoice,
        nativeToolNames,
      );
    }
  } else if (
    nativeChoice !== undefined
    || (route.rawModel.api === "openai-codex-responses" && generation.maxTokens !== undefined)
  ) {
    options.onPayload = (payload) => {
      if (!plainObject(payload)) throw new Error("native request payload is not an object");
      return {
        ...payload,
        ...(nativeChoice === undefined ? {} : { tool_choice: nativeChoice }),
        ...(route.rawModel.api === "openai-codex-responses" && generation.maxTokens !== undefined
          ? { max_output_tokens: generation.maxTokens }
          : {}),
      };
    };
  }
  if (route.rawModel.api === "openai-codex-responses") options.transport = "sse";
  return options;
}

function canonicalizeAnthropicPayload(payload, tools, choice, nativeToolNames) {
  if (!plainObject(payload) || !Array.isArray(payload.tools)) {
    throw new Error("native Anthropic payload has no tools");
  }
  const expected = new Map(tools.map((tool) => [tool.name.toLowerCase(), tool]));
  if (expected.size !== tools.length || payload.tools.length !== tools.length) {
    throw new Error("native Anthropic payload changed the declared tools");
  }
  const seen = new Set();
  const nativeTools = payload.tools.map((native) => {
    if (!plainObject(native) || typeof native.name !== "string") {
      throw new Error("native Anthropic payload contains an invalid tool");
    }
    const key = native.name.toLowerCase();
    const source = expected.get(key);
    if (!source || seen.has(key)) {
      throw new Error("native Anthropic payload changed the declared tools");
    }
    seen.add(key);
    nativeToolNames.set(native.name, source.name);
    return { ...native, input_schema: structuredClone(source.parameters) };
  });
  const result = { ...payload, tools: nativeTools };
  if (choice.mode === ToolChoiceMode.SPECIFIC) {
    const selected = nativeTools.find(
      (tool) => tool.name.toLowerCase() === choice.toolName.toLowerCase(),
    );
    if (!selected) throw new Error("native Anthropic payload omitted the selected tool");
    result.tool_choice = { type: "tool", name: selected.name };
  }
  return result;
}

function toolChoice(api, choice) {
  switch (choice.mode) {
    case ToolChoiceMode.AUTO:
      return undefined;
    case ToolChoiceMode.NONE:
      return "none";
    case ToolChoiceMode.REQUIRED:
      return api === "anthropic-messages" ? "any" : "required";
    case ToolChoiceMode.SPECIFIC:
      return api === "anthropic-messages"
        ? { type: "tool", name: choice.toolName }
        : { type: "function", name: choice.toolName };
    default:
      throw new ConnectError("unsupported tool choice", Code.InvalidArgument);
  }
}

function portableUsage(value) {
  if (!value) return undefined;
  const rawInput = tokenCount(value.input, "input");
  const cached = tokenCount(value.cacheRead, "cacheRead");
  const cacheWrite = tokenCount(value.cacheWrite, "cacheWrite");
  const output = tokenCount(value.output, "output");
  const input = rawInput + cached + cacheWrite;
  const usage = {
    inputTokens: input,
    outputTokens: output,
    totalTokens: input + output,
    cachedInputTokens: cached,
  };
  if (value.reasoning !== undefined) {
    const reasoning = tokenCount(value.reasoning, "reasoning");
    if (reasoning > output) throw malformed("native reasoning usage exceeds output usage");
    usage.reasoningTokens = reasoning;
  }
  return usage;
}

function tokenCount(value, field) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw malformed(`native ${field} usage is invalid`);
  }
  return BigInt(value);
}

function finishReason(reason) {
  switch (reason) {
    case "stop": return FinishReason.STOP;
    case "length": return FinishReason.MAX_OUTPUT_TOKENS;
    case "toolUse": return FinishReason.TOOL_CALLS;
    default: throw malformed("native finish reason is unsupported");
  }
}

function publicToolId(api, value) {
  return api.includes("responses") ? value.split("|", 1)[0] : value;
}

function validateToolIdentity(id, name, prepared, secrets, used) {
  if (
    !CALL_ID.test(id)
    || used.has(id)
  ) {
    throw malformed("native tool call id is invalid or duplicated");
  }
  if (typeof name !== "string" || !TOOL_NAME.test(name) || !prepared.context.tools?.some((tool) => tool.name === name)) {
    throw malformed("native tool call names an undeclared tool");
  }
  if (redactValue(id, secrets) !== id || redactValue(name, secrets) !== name) {
    throw malformed("native tool call metadata contains a credential");
  }
}

function textDelta(index, value) {
  return {
    event: {
      case: "itemDelta",
      value: { index, delta: { case: "text", value } },
    },
  };
}

function requireStarted(started) {
  if (!started) throw malformed("native output preceded start");
}

function malformed(message) {
  return new ConnectError(`invalid native stream: ${message}`, Code.DataLoss);
}

function sanitizedFailure(_error, signal) {
  if (signal?.aborted) {
    if (signal.reason instanceof ConnectError) return signal.reason;
    return new ConnectError("request canceled", Code.Canceled);
  }
  return new ConnectError("upstream inference failed", Code.Unavailable);
}

function plainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
