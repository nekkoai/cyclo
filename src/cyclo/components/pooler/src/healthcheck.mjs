#!/usr/bin/env node

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { createClient } from "@connectrpc/connect";
import { Component, HealthStatus } from "@cyclo/component/contract";
import { LOCAL_COMPONENT_TARGET } from "@cyclo/component/links";
import { createDockerTransport } from "@cyclo/component/transport";

export async function checkPoolerHealth({
  target = LOCAL_COMPONENT_TARGET,
  timeoutMs = 1_000,
} = {}) {
  const client = createClient(Component, createDockerTransport(target));
  const response = await client.health({}, { timeoutMs });
  if (response.status !== HealthStatus.READY) {
    const detail = response.message.trim() || `status ${response.status}`;
    throw new Error(`pooler is not ready: ${detail}`);
  }
  return true;
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
  checkPoolerHealth()
    .then(() => { process.exitCode = 0; })
    .catch((error) => {
      console.error(error instanceof Error ? error.message : String(error));
      process.exitCode = 1;
    });
}
