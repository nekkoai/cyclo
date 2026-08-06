#!/usr/bin/env node

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Code, ConnectError } from "@connectrpc/connect";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";

import { aggregateUsageFile } from "./audit.mjs";
import { formatSupportedProviders } from "./providers.mjs";
import { createGatewayServer } from "./server.mjs";

const DEFAULT_USAGE_PATH = "/var/lib/cyclo-gateway/usage.jsonl";

export async function runGateway({
  services,
  createServices = loadDefaultServices,
  env = process.env,
  signalSource = process,
  listenOptions,
  onListening,
} = {}) {
  const shutdown = new AbortController();
  let notifySignal;
  const signaled = new Promise((resolve) => {
    notifySignal = resolve;
  });
  const onSignal = (name) => {
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("gateway is shutting down", Code.Unavailable));
    }
    notifySignal(name);
  };
  const onSigterm = () => onSignal("SIGTERM");
  const onSigint = () => onSignal("SIGINT");
  signalSource.once("SIGTERM", onSigterm);
  signalSource.once("SIGINT", onSigint);

  let server;
  try {
    const resolvedServices = services
      ?? await createServices({ env, signal: shutdown.signal });
    if (shutdown.signal.aborted) return;

    server = await createGatewayServer({
      services: resolvedServices,
      shutdownSignal: shutdown.signal,
    });
    const address = await listenComponentServer(server, listenOptions);
    onListening?.(address);

    await Promise.race([signaled, serverFailure(server)]);
  } finally {
    signalSource.removeListener("SIGTERM", onSigterm);
    signalSource.removeListener("SIGINT", onSigint);
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("gateway stopped", Code.Unavailable));
    }
    if (server) await closeComponentServer(server);
  }
}

export async function main(argv = process.argv.slice(2), options = {}) {
  const env = options.env ?? process.env;
  const input = options.input ?? process.stdin;
  const output = options.output ?? process.stdout;
  const command = argv[0];
  if (command === undefined) {
    await (options.runGateway ?? runGateway)({ env });
    return;
  }
  if (command === "login" && argv.length >= 2) {
    const loginCommand = options.login
      ?? (await import("./login.mjs")).login;
    await loginCommand(argv.slice(1), { env, input, output });
    return;
  }
  if (command === "providers" && argv.length === 1) {
    output.write(`${(options.formatProviders ?? formatSupportedProviders)()}\n`);
    return;
  }
  if (command === "usage" && argv.length === 1) {
    const path = env.CYCLO_GATEWAY_USAGE_JSONL ?? DEFAULT_USAGE_PATH;
    const report = await (options.aggregateUsage ?? aggregateUsageFile)(path);
    output.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  throw new Error(
    "usage: cyclo-gateway-component [providers | usage | login PROVIDER [OPTIONS]]",
  );
}

async function loadDefaultServices(options) {
  const { createGatewayServices } = await import("./services.mjs");
  return createGatewayServices(options);
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
