import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createClient } from "@connectrpc/connect";
import { connectNodeAdapter } from "@connectrpc/connect-node";
import { Provider } from "@cyclo/provider/contract";
import { createUnixTransport } from "@cyclo/component/transport";

test("ConnectRPC preserves opaque request and streamed response strings", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-provider-connect-"));
  const socketPath = join(directory, "provider.sock");
  const requestPayload = " { \"schema\": {\"anyOf\":[true,false]}, \"order\": 1 } ";
  const responsePayloads = [
    "{\"type\":\"start\",\"unknown\":1}",
    " { \"type\": \"future_event\", \"payload\": [1,2,3] } ",
  ];
  let observed;
  const server = createServer(connectNodeAdapter({
    connect: true,
    grpc: false,
    grpcWeb: false,
    routes(router) {
      router.service(Provider, {
        listModels() { return { models: [] }; },
        async *infer(request) {
          observed = request;
          for (const payload of responsePayloads) yield { payload };
        },
      });
    },
  }));

  try {
    await listen(server, socketPath);
    const client = createClient(Provider, createUnixTransport(socketPath));
    const actual = [];
    for await (const response of client.infer({ model: "route/model", payload: requestPayload })) {
      actual.push(response.payload);
    }
    assert.equal(observed.model, "route/model");
    assert.equal(observed.payload, requestPayload);
    assert.deepEqual(actual, responsePayloads);
  } finally {
    await close(server);
    await rm(directory, { recursive: true, force: true });
  }
});

test("ConnectRPC propagates cancellation outside the payload", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-provider-cancel-"));
  const socketPath = join(directory, "provider.sock");
  const canceled = deferred();
  const server = createServer(connectNodeAdapter({
    connect: true,
    grpc: false,
    grpcWeb: false,
    routes(router) {
      router.service(Provider, {
        listModels() { return { models: [] }; },
        async *infer(_request, context) {
          try {
            yield { payload: "first" };
            await aborted(context.signal);
            throw context.signal.reason;
          } finally {
            canceled.resolve();
          }
        },
      });
    },
  }));

  try {
    await listen(server, socketPath);
    const client = createClient(Provider, createUnixTransport(socketPath));
    const controller = new AbortController();
    const iterator = client.infer(
      { model: "route/model", payload: "opaque" },
      { signal: controller.signal },
    )[Symbol.asyncIterator]();
    assert.equal((await iterator.next()).value.payload, "first");
    controller.abort();
    await assert.rejects(iterator.next());
    await canceled.promise;
  } finally {
    await close(server);
    await rm(directory, { recursive: true, force: true });
  }
});

function listen(server, path) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(path, () => {
      server.off("error", reject);
      resolve();
    });
  });
}

function close(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
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
