#!/usr/bin/env node

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Code, ConnectError } from "@connectrpc/connect";
import { componentSocketPath } from "@cyclo/component/paths";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";

import { createPassthroughServices } from "./services.mjs";
import { createPassthroughServer } from "./server.mjs";
import { createUpstreamBinding } from "./upstream.mjs";

export async function runPassthrough({
  env = process.env,
  signalSource = process,
  createUpstream = createUpstreamBinding,
} = {}) {
  const shutdown = new AbortController();
  let notifySignal;
  const signaled = new Promise((resolve) => {
    notifySignal = resolve;
  });
  const onSignal = () => {
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("passthrough is shutting down", Code.Unavailable));
    }
    notifySignal();
  };
  signalSource.once("SIGTERM", onSignal);
  signalSource.once("SIGINT", onSignal);

  let server;
  try {
    const upstream = await createUpstream({ env });
    if (shutdown.signal.aborted) return;
    const services = createPassthroughServices({ upstream });
    server = await createPassthroughServer({
      services,
      shutdownSignal: shutdown.signal,
    });
    await listenComponentServer(server, {
      socketPath: componentSocketPath(env),
    });
    await Promise.race([signaled, serverFailure(server)]);
  } finally {
    signalSource.removeListener("SIGTERM", onSignal);
    signalSource.removeListener("SIGINT", onSignal);
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("passthrough stopped", Code.Unavailable));
    }
    if (server) await closeComponentServer(server);
  }
}

export async function main(argv = process.argv.slice(2)) {
  const command = argv[0] ?? "serve";
  if (command !== "serve" || argv.length > 1) {
    throw new Error("usage: cyclo-passthrough-component serve");
  }
  await runPassthrough();
}

function serverFailure(server) {
  return new Promise((_, reject) => server.once("error", reject));
}

function isMain() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (isMain()) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
