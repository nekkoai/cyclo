import { chmod, lstat, mkdir, unlink } from "node:fs/promises";
import { createServer } from "node:http";
import { createConnection } from "node:net";
import { dirname, isAbsolute } from "node:path";

import { connectNodeAdapter } from "@connectrpc/connect-node";

import { registerProvides } from "./bindings.mjs";

const closePromises = new WeakMap();

// The component must be the only writer of the socket directory. Node unlinks
// its Unix-socket pathname on close; consumers therefore mount the directory
// read-only. Mount possession is the capability: the socket is connectable by
// arbitrary non-root image users, while an unmounted container cannot name it.

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

export async function listenComponentServer(server, { socketPath, mode = 0o666 } = {}) {
  if (typeof socketPath !== "string" || !isAbsolute(socketPath)) {
    throw new TypeError("socketPath must be an absolute path");
  }
  await mkdir(dirname(socketPath), { recursive: true });
  await removeStaleSocket(socketPath);
  await listen(server, socketPath);

  try {
    await chmod(socketPath, mode);
  } catch (error) {
    await closeNodeServer(server);
    throw error;
  }
  return socketPath;
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

async function removeStaleSocket(socketPath) {
  const before = await socketIdentity(socketPath, { missing: true });
  if (!before) return;
  if (!before.isSocket) throw new Error(`refusing to replace non-socket path ${socketPath}`);

  let connectError;
  try {
    await connectToSocket(socketPath);
  } catch (error) {
    connectError = error;
  }
  if (!connectError) {
    const error = new Error(`component socket is already in use: ${socketPath}`);
    error.code = "EADDRINUSE";
    throw error;
  }
  if (connectError.code === "ENOENT") return;
  if (connectError.code !== "ECONNREFUSED") throw connectError;

  const after = await socketIdentity(socketPath, { missing: true });
  if (!after) return;
  if (!sameSocket(before, after)) {
    throw new Error(`socket changed while checking whether it was stale: ${socketPath}`);
  }
  await unlink(socketPath).catch(ignoreMissing);
}

function connectToSocket(socketPath) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(socketPath);
    socket.once("connect", () => {
      socket.destroy();
      resolve();
    });
    socket.once("error", (error) => {
      socket.destroy();
      reject(error);
    });
  });
}

function listen(server, socketPath) {
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
    server.listen(socketPath);
  });
}

function closeNodeServer(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function socketIdentity(socketPath, { missing = false } = {}) {
  try {
    const stat = await lstat(socketPath, { bigint: true });
    return {
      path: socketPath,
      dev: stat.dev,
      ino: stat.ino,
      isSocket: stat.isSocket(),
    };
  } catch (error) {
    if (missing && error?.code === "ENOENT") return undefined;
    throw error;
  }
}

function sameSocket(left, right) {
  return right.isSocket && left.dev === right.dev && left.ino === right.ino;
}

function ignoreMissing(error) {
  if (error?.code !== "ENOENT") throw error;
}
