import { createClient } from "@connectrpc/connect";

import { Component, HealthStatus } from "../gen/cyclo/component/v1/component_pb.js";
import { componentSocketPath } from "./paths.mjs";
import { createUnixTransport } from "./transport.mjs";

export async function checkComponentHealth({
  socketPath = componentSocketPath(),
  timeoutMs = 1_000,
} = {}) {
  const client = createClient(Component, createUnixTransport(socketPath));
  const response = await client.health({}, { timeoutMs });
  return response.status === HealthStatus.READY;
}
