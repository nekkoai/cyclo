#!/usr/bin/env node

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Code, ConnectError } from "@connectrpc/connect";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";

import { parseArguments, parseComponentName } from "./config.mjs";
import { createPoolerServer } from "./server.mjs";
import { createPoolerServices } from "./services.mjs";
import { createUpstreamBinding } from "./upstream.mjs";

export async function runPooler({
  argv = process.argv.slice(2),
  env = process.env,
  signalSource = process,
  createUpstream = createUpstreamBinding,
  listenOptions,
  onListening,
} = {}) {
  const config = parseArguments(argv);
  const componentName = parseComponentName(env.DCOMP_COMPONENT_NAME);
  const shutdown = new AbortController();
  let resolveSignal;
  const signaled = new Promise((resolve) => { resolveSignal = resolve; });
  const onSignal = () => {
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("pooler is shutting down", Code.Unavailable));
    }
    resolveSignal();
  };
  signalSource.once("SIGTERM", onSignal);
  signalSource.once("SIGINT", onSignal);

  let server;
  try {
    const upstream = await createUpstream({ env });
    if (shutdown.signal.aborted) return;
    const services = createPoolerServices({ upstream, config, componentName });
    server = await createPoolerServer({
      services,
      shutdownSignal: shutdown.signal,
    });
    const address = await listenComponentServer(server, listenOptions);
    onListening?.(address);
    await Promise.race([signaled, serverFailure(server)]);
  } finally {
    signalSource.removeListener("SIGTERM", onSignal);
    signalSource.removeListener("SIGINT", onSignal);
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("pooler stopped", Code.Unavailable));
    }
    if (server) await closeComponentServer(server);
  }
}

export async function main(argv = process.argv.slice(2)) {
  try {
    await runPooler({ argv });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error(
        "usage: cyclo-pooler-component PROVIDER PROVIDER...\n"
        + "   or: cyclo-pooler-component MEMBER_MODEL MEMBER_MODEL... "
        + "model=OUTPUT_MODEL\n"
        + error.message,
      );
    }
    throw error;
  }
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
