import assert from "node:assert/strict";
import test from "node:test";

import { Component, HealthStatus } from "@cyclo/component/contract";

import { resolveBindings } from "../src/bindings.mjs";
import { parseDeclaration } from "../src/declaration.mjs";
import { checkComponentHealth } from "../src/health.mjs";
import {
  COMPONENT_HOST,
  COMPONENT_PORT,
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "../src/server.mjs";

test("component listener defaults match the DComp contract", () => {
  assert.equal(COMPONENT_HOST, "0.0.0.0");
  assert.equal(COMPONENT_PORT, 50051);
});

test("listens on TCP and close is idempotent", async () => {
  const server = healthServer();
  try {
    const address = await listenComponentServer(server, {
      host: "127.0.0.1",
      port: 0,
    });
    const target = `dns:///127.0.0.1:${address.port}`;
    assert.equal(await checkComponentHealth({ target }), true);
    await Promise.all([
      closeComponentServer(server),
      closeComponentServer(server),
    ]);
    await assert.rejects(checkComponentHealth({ target, timeoutMs: 50 }));
  } finally {
    await closeComponentServer(server);
  }
});

test("rejects invalid listener options before opening a port", async () => {
  for (const options of [
    { host: "" },
    { port: -1 },
    { port: 65_536 },
    { port: 1.5 },
  ]) {
    const server = healthServer();
    await assert.rejects(listenComponentServer(server, options), /host|port/u);
    assert.equal(server.listening, false);
  }
});

function healthServer() {
  const bindings = resolveBindings(
    parseDeclaration(`
      component test-health
      provide cyclo.component.v1.Component
    `),
    [Component],
  );
  return createComponentServer({
    bindings,
    implementations: new Map([[Component.typeName, {
      health() {
        return { status: HealthStatus.READY, message: "ready" };
      },
    }]]),
  });
}
