import { Code, ConnectError } from "@connectrpc/connect";

import { FinishReason } from "../gen/cyclo/provider/v1/provider_pb.js";

const ITEM_KINDS = new Set([
  "text",
  "reasoningSummary",
  "toolCall",
  "media",
  "extension",
]);
const DELTA_KINDS = new Set(["text", "toolArgumentsJson", "media"]);
const FINISH_REASONS = new Set([
  FinishReason.STOP,
  FinishReason.STOP_SEQUENCE,
  FinishReason.MAX_OUTPUT_TOKENS,
  FinishReason.TOOL_CALLS,
  FinishReason.CONTENT_FILTER,
  FinishReason.REFUSAL,
  FinishReason.OTHER,
]);

// Validate an upstream Provider stream while forwarding it. Explicit Connect
// errors pass through unchanged; malformed clean streams become DATA_LOSS.
export async function* validateInferStream(responses, { model } = {}) {
  if (!responses || typeof responses[Symbol.asyncIterator] !== "function") {
    throw new TypeError("responses must be an AsyncIterable");
  }

  let started = false;
  let finished = false;
  let finalResponse;
  let nextIndex = 0;
  const open = new Map();
  const toolCallIds = new Set();

  for await (const response of responses) {
    const event = response?.event;
    if (!event || typeof event.case !== "string" || event.value === undefined) {
      throw protocolError("response has no event");
    }

    if (!started) {
      if (event.case !== "started") {
        throw protocolError("Started must be the first event");
      }
      if (!event.value.responseId || !event.value.model) {
        throw protocolError("Started requires response_id and model");
      }
      if (model !== undefined && event.value.model !== model) {
        throw protocolError("Started.model does not match the requested model");
      }
      validateExtensions(event.value.extensions, "Started");
      started = true;
      yield response;
      continue;
    }

    if (finished) throw protocolError("event follows Finished");

    switch (event.case) {
      case "itemStarted": {
        const { index, item } = event.value;
        if (!Number.isSafeInteger(index) || index !== nextIndex) {
          throw protocolError(`expected item index ${nextIndex}`);
        }
        if (!item || !ITEM_KINDS.has(item.case) || item.value === undefined) {
          throw protocolError(`item ${index} has no valid kind`);
        }
        if (item.case === "toolCall") {
          if (!item.value.id || !item.value.name) {
            throw protocolError(`tool-call item ${index} requires id and name`);
          }
          if (toolCallIds.has(item.value.id)) {
            throw protocolError(`duplicate tool-call id ${item.value.id}`);
          }
          toolCallIds.add(item.value.id);
        }
        if (item.case === "media" && !item.value.mediaType) {
          throw protocolError(`media item ${index} requires media_type`);
        }
        if (item.case === "extension" && !item.value.typeUrl) {
          throw protocolError(`extension item ${index} requires type_url`);
        }
        open.set(index, item.case);
        nextIndex += 1;
        break;
      }
      case "itemDelta": {
        const { index, delta } = event.value;
        const kind = open.get(index);
        if (kind === undefined) throw protocolError(`delta for closed or unknown item ${index}`);
        if (!delta || delta.value === undefined) {
          throw protocolError(`item ${index} has no valid delta`);
        }
        if (delta.case === "text" && kind !== "text" && kind !== "reasoningSummary") {
          throw protocolError(`text delta does not belong to item ${index}`);
        }
        if (delta.case === "toolArgumentsJson" && kind !== "toolCall") {
          throw protocolError(`tool-argument delta does not belong to item ${index}`);
        }
        if (delta.case === "media" && kind !== "media") {
          throw protocolError(`media delta does not belong to item ${index}`);
        }
        if (!DELTA_KINDS.has(delta.case)) {
          throw protocolError(`item ${index} has unknown delta kind`);
        }
        break;
      }
      case "itemFinished": {
        const { index, toolArguments } = event.value;
        const kind = open.get(index);
        if (kind === undefined) throw protocolError(`finish for closed or unknown item ${index}`);
        if (kind === "toolCall" && toolArguments === undefined) {
          throw protocolError(`tool-call item ${index} has no final arguments`);
        }
        if (kind !== "toolCall" && toolArguments !== undefined) {
          throw protocolError(`non-tool item ${index} has tool arguments`);
        }
        validateExtensions(event.value.extensions, `item ${index}`);
        open.delete(index);
        break;
      }
      case "finished": {
        if (open.size !== 0) throw protocolError("Finished has open items");
        if (!FINISH_REASONS.has(event.value.reason)) {
          throw protocolError("Finished has an invalid reason");
        }
        const hasStopSequence = event.value.stopSequence !== undefined;
        if ((event.value.reason === FinishReason.STOP_SEQUENCE) !== hasStopSequence) {
          throw protocolError("stop_sequence does not match the finish reason");
        }
        if (hasStopSequence && event.value.stopSequence.length === 0) {
          throw protocolError("stop_sequence is empty");
        }
        validateUsage(event.value.usage);
        validateExtensions(event.value.extensions, "Finished");
        finished = true;
        finalResponse = response;
        continue;
      }
      default:
        throw protocolError(`unexpected ${event.case} event`);
    }

    yield response;
  }

  if (!started) throw protocolError("stream ended before Started");
  if (!finished) throw protocolError("stream ended before Finished");
  yield finalResponse;
}

function validateExtensions(extensions, location) {
  for (const extension of extensions ?? []) {
    if (!extension?.typeUrl) throw protocolError(`${location} has an extension without type_url`);
  }
}

function validateUsage(usage) {
  if (usage === undefined) return;
  const { inputTokens, outputTokens, totalTokens, cachedInputTokens, reasoningTokens } = usage;
  if (totalTokens !== undefined) {
    if (inputTokens === undefined || outputTokens === undefined) {
      throw protocolError("total usage requires input and output token counts");
    }
    if (totalTokens !== inputTokens + outputTokens) {
      throw protocolError("total token count is inconsistent");
    }
  }
  if (cachedInputTokens !== undefined) {
    if (inputTokens === undefined || cachedInputTokens > inputTokens) {
      throw protocolError("cached input tokens are inconsistent");
    }
  }
  if (reasoningTokens !== undefined) {
    if (outputTokens === undefined || reasoningTokens > outputTokens) {
      throw protocolError("reasoning tokens are inconsistent");
    }
  }
}

function protocolError(message) {
  return new ConnectError(`invalid Provider stream: ${message}`, Code.DataLoss);
}
