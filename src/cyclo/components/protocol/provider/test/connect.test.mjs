import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { createClient } from "@connectrpc/connect";
import { connectNodeAdapter } from "@connectrpc/connect-node";
import { Provider } from "@cyclo/provider/contract";
import { createDockerTransport } from "@cyclo/component/transport";

test("ConnectRPC preserves opaque request and streamed response strings", async () => {
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
    const port = await listen(server);
    const client = createClient(
      Provider,
      createDockerTransport(`dns:///127.0.0.1:${port}`),
    );
    const actual = [];
    for await (const response of client.infer({ model: "route/model", payload: requestPayload })) {
      actual.push(response.payload);
    }
    assert.equal(observed.model, "route/model");
    assert.equal(observed.payload, requestPayload);
    assert.deepEqual(actual, responsePayloads);
  } finally {
    await close(server);
  }
});

test("ConnectRPC propagates cancellation outside the payload", async () => {
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
    const port = await listen(server);
    const client = createClient(
      Provider,
      createDockerTransport(`dns:///127.0.0.1:${port}`),
    );
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
  }
});

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
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
