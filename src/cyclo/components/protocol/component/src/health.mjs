import { createClient } from "@connectrpc/connect";

import { Component, HealthStatus } from "../gen/cyclo/component/v1/component_pb.js";
import { LOCAL_COMPONENT_TARGET } from "./links.mjs";
import { createDockerTransport } from "./transport.mjs";

export async function checkComponentHealth({
  target = LOCAL_COMPONENT_TARGET,
  timeoutMs = 1_000,
} = {}) {
  const client = createClient(Component, createDockerTransport(target));
  const response = await client.health({}, { timeoutMs });
  return response.status === HealthStatus.READY;
}
