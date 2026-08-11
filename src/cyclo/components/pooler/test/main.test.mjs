import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { createClient } from "@connectrpc/connect";
import { createDockerTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";
import { PI_INFERENCE_FORMAT } from "@cyclo/provider/protocol";

import { main, runPooler } from "../src/main.mjs";

test("the image command is the pool configuration, with no serve subcommand", async () => {
  await assert.rejects(main([]), /usage: cyclo-pooler-component/u);
  await assert.rejects(main(["serve"]), /at least two members/u);
});

test("SIGTERM cancels active inference and closes the component listener", async () => {
  const signalSource = new EventEmitter();
  const listening = deferred();
  const canceled = deferred();
  const upstream = {
    client: {
      listModels: async () => ({ models: models() }),
      async *infer(_request, options) {
        yield { payload: "first" };
        try {
          await aborted(options.signal);
          throw options.signal.reason;
        } finally {
          canceled.resolve();
        }
      },
    },
    callOptions(signal, timeoutMs) {
      const options = { signal };
      if (timeoutMs !== undefined) options.timeoutMs = timeoutMs;
      return options;
    },
  };
  const running = runPooler({
    argv: ["one/model", "two/model", "model=balanced"],
    env: { DCOMP_COMPONENT_NAME: "pool" },
    signalSource,
    createUpstream: async () => upstream,
    listenOptions: { host: "127.0.0.1", port: 0 },
    onListening: listening.resolve,
  });

  const address = await listening.promise;
  const client = createClient(
    Provider,
    createDockerTransport(`dns:///127.0.0.1:${address.port}`),
  );
  const iterator = client.infer({ model: "pool/balanced", payload: "opaque" })[
    Symbol.asyncIterator
  ]();
  assert.equal((await iterator.next()).value.payload, "first");
  signalSource.emit("SIGTERM");
  await assert.rejects(iterator.next());
  await running;
  await withTimeout(canceled.promise);
  assert.equal(signalSource.listenerCount("SIGTERM"), 0);
  assert.equal(signalSource.listenerCount("SIGINT"), 0);
});

function models() {
  return [model("one/model"), model("two/model")];
}

function model(id) {
  return {
    id,
    displayName: id,
    capabilities: {
      inputModalities: [1],
      outputModalities: [1],
      functionTools: true,
      parallelToolCalls: true,
      reasoningSummaries: true,
      temperature: false,
      topP: false,
      stopSequences: false,
      extensionTypes: [],
      reasoning: true,
    },
    contextWindowTokens: 100_000n,
    maxOutputTokens: 8_000n,
    extensions: [],
    inferenceFormat: PI_INFERENCE_FORMAT,
  };
}

function aborted(signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function withTimeout(promise, timeoutMs = 1_000) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timed out")), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}
