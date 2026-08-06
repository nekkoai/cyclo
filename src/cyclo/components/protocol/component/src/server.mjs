import { createServer } from "node:http";

import { connectNodeAdapter } from "@connectrpc/connect-node";

import { registerProvides } from "./bindings.mjs";

export const COMPONENT_HOST = "0.0.0.0";
export const COMPONENT_PORT = 50051;

const closePromises = new WeakMap();

export function createComponentServer({ bindings, implementations, shutdownSignal } = {}) {
  if (!bindings?.provides || !bindings?.requires) {
    throw new TypeError("resolved component bindings are required");
  }
  return createServer(
    connectNodeAdapter({
      connect: true,
      grpc: false,
      grpcWeb: false,
      shutdownSignal,
      routes(router) {
        registerProvides(router, bindings, implementations);
      },
    }),
  );
}

export async function listenComponentServer(
  server,
  { host = COMPONENT_HOST, port = COMPONENT_PORT } = {},
) {
  if (typeof host !== "string" || !host) {
    throw new TypeError("host must be a non-empty string");
  }
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new TypeError("port must be an integer between 0 and 65535");
  }
  await listen(server, host, port);
  return server.address();
}

export function closeComponentServer(server) {
  const existing = closePromises.get(server);
  if (existing) return existing;

  const closing = closeNodeServer(server).finally(() => {
    closePromises.delete(server);
  });
  closePromises.set(server, closing);
  return closing;
}

function listen(server, host, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, host);
  });
}

function closeNodeServer(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}
