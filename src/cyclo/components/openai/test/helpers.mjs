import { Modality } from "@cyclo/provider/contract";
import { encodePayload, PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

export function providerModel(overrides = {}) {
  const capabilities = {
    inputModalities: [Modality.TEXT, Modality.IMAGE],
    outputModalities: [Modality.TEXT],
    functionTools: true,
    parallelToolCalls: true,
    reasoningSummaries: true,
    temperature: true,
    topP: true,
    stopSequences: false,
    extensionTypes: [],
    reasoning: true,
    ...overrides.capabilities,
  };
  return {
    id: "work/test-model",
    displayName: "Test model",
    capabilities,
    contextWindowTokens: 16_384n,
    maxOutputTokens: 4_096n,
    extensions: [],
    inferenceFormat: PI_INFERENCE_FORMAT,
    ...overrides,
    capabilities,
  };
}

export function assistant(content = [], overrides = {}) {
  return {
    role: "assistant",
    content,
    api: "fixture",
    provider: "work",
    model: "test-model",
    usage: {
      input: 11,
      output: 7,
      cacheRead: 3,
      cacheWrite: 2,
      totalTokens: 23,
      cost: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        total: 0,
      },
    },
    stopReason: "stop",
    timestamp: 1_700_000_000_000,
    ...overrides,
  };
}

export function textEvents(text = "hello") {
  const partial = assistant([{ type: "text", text: "" }]);
  const complete = assistant([{ type: "text", text }]);
  return [
    { type: "start", partial },
    { type: "text_start", contentIndex: 0, partial },
    { type: "text_delta", contentIndex: 0, delta: text, partial: complete },
    { type: "text_end", contentIndex: 0, content: text, partial: complete },
    { type: "done", reason: "stop", message: complete },
  ];
}

export function providerClient({
  models = [providerModel()],
  events = textEvents(),
  onInfer,
  inferError,
  listError,
} = {}) {
  return {
    async listModels(_request, options) {
      if (listError) throw listError;
      return { models, options };
    },
    async *infer(request, options) {
      onInfer?.(request, options);
      if (inferError) throw inferError;
      for (const event of events) yield { payload: encodePayload(event) };
    },
  };
}

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
