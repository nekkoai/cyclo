#!/usr/bin/env node

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Code, ConnectError } from "@connectrpc/connect";
import {
  closeComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";

import { createOpenAIHTTPServer } from "./http.mjs";
import { createOpenAIComponentServer } from "./server.mjs";
import { createOpenAIServices } from "./services.mjs";
import { createProviderBinding } from "./upstream.mjs";

export const OPENAI_HTTP_HOST = "0.0.0.0";
export const OPENAI_HTTP_PORT = 8080;

export async function runOpenAI({
  env = process.env,
  signalSource = process,
  createProvider = createProviderBinding,
  createServices = createOpenAIServices,
  createComponentServer = createOpenAIComponentServer,
  createHTTPServer = createOpenAIHTTPServer,
  componentListenOptions,
  httpListenOptions,
  onListening = ({ component, openai }) => {
    console.error(
      `Cyclo OpenAI component listening on component port ${component.port} `
      + `and http://${displayHost(openai.address)}:${openai.port}/v1`,
    );
  },
} = {}) {
  const shutdown = new AbortController();
  let notifySignal;
  const signaled = new Promise((resolve) => {
    notifySignal = resolve;
  });
  const onSignal = (name) => {
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError(
        `openai component received ${name}`,
        Code.Unavailable,
      ));
    }
    notifySignal(name);
  };
  const onSigterm = () => onSignal("SIGTERM");
  const onSigint = () => onSignal("SIGINT");
  signalSource.once("SIGTERM", onSigterm);
  signalSource.once("SIGINT", onSigint);

  let componentServer;
  let httpServer;
  try {
    const provider = await createProvider({ env });
    if (shutdown.signal.aborted) return;
    const services = createServices({ provider });
    componentServer = await createComponentServer({
      services,
      shutdownSignal: shutdown.signal,
    });
    httpServer = createHTTPServer({
      client: provider.client,
      apiKey: environmentAPIKey(env.CYCLO_OPENAI_API_KEY),
      shutdownSignal: shutdown.signal,
    });

    const componentAddress = await listenComponentServer(
      componentServer,
      componentListenOptions,
    );
    const openAIAddress = await listenHTTPServer(httpServer, {
      host: httpListenOptions?.host ?? nonEmpty(env.CYCLO_OPENAI_HOST) ?? OPENAI_HTTP_HOST,
      port: httpListenOptions?.port ?? environmentPort(env.CYCLO_OPENAI_PORT),
    });
    onListening?.({ component: componentAddress, openai: openAIAddress });
    await Promise.race([
      signaled,
      serverFailure(componentServer),
      serverFailure(httpServer),
    ]);
  } finally {
    signalSource.removeListener("SIGTERM", onSigterm);
    signalSource.removeListener("SIGINT", onSigint);
    if (!shutdown.signal.aborted) {
      shutdown.abort(new ConnectError("openai component stopped", Code.Unavailable));
    }
    await Promise.all([
      componentServer ? closeComponentServer(componentServer) : Promise.resolve(),
      httpServer ? closeHTTPServer(httpServer) : Promise.resolve(),
    ]);
  }
}

export async function main(argv = process.argv.slice(2), options = {}) {
  if (argv.length !== 0) throw new Error("usage: cyclo-openai-component");
  await runOpenAI(options);
}

function environmentPort(raw) {
  if (raw === undefined || raw === "") return OPENAI_HTTP_PORT;
  const port = Number(raw);
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new TypeError("CYCLO_OPENAI_PORT must be an integer between 0 and 65535");
  }
  return port;
}

function nonEmpty(value) {
  return typeof value === "string" && value ? value : undefined;
}

function environmentAPIKey(value) {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value) {
    throw new TypeError("CYCLO_OPENAI_API_KEY must be a non-empty string when set");
  }
  return value;
}

function displayHost(address) {
  return ["0.0.0.0", "::"].includes(address) ? "127.0.0.1" : address;
}

function listenHTTPServer(server, { host, port }) {
  if (typeof host !== "string" || !host) {
    throw new TypeError("OpenAI HTTP host must be a non-empty string");
  }
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new TypeError("OpenAI HTTP port must be an integer between 0 and 65535");
  }
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("listening", onReady);
      reject(error);
    };
    const onReady = () => {
      server.off("error", onError);
      resolve(server.address());
    };
    server.once("error", onError);
    server.once("listening", onReady);
    server.listen(port, host);
  });
}

function closeHTTPServer(server) {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
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
