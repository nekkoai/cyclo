import { isAbsolute } from "node:path";

import { createConnectTransport } from "@connectrpc/connect-node";

export function createUnixTransport(socketPath) {
  if (typeof socketPath !== "string" || !isAbsolute(socketPath)) {
    throw new TypeError("socketPath must be an absolute path");
  }
  return createConnectTransport({
    baseUrl: "http://localhost",
    httpVersion: "1.1",
    nodeOptions: { socketPath },
  });
}
