import assert from "node:assert/strict";
import test from "node:test";

import { createClient } from "@connectrpc/connect";

import { Component, HealthStatus } from "@cyclo/component/contract";
import { resolveBindings } from "../src/bindings.mjs";
import { parseDeclaration } from "../src/declaration.mjs";
import { checkComponentHealth } from "../src/health.mjs";
import {
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "../src/server.mjs";
import { createDockerTransport } from "../src/transport.mjs";

test("generated handler and client communicate over ConnectRPC TCP", async () => {
  const declaration = parseDeclaration(`
    component health-proxy
    provide cyclo.component.v1.Component
    require upstream cyclo.component.v1.Component
  `);
  const bindings = resolveBindings(declaration, [Component]);
  const provided = bindings.provides.get(Component.typeName);
  const required = bindings.requires.get("upstream");
  let status = HealthStatus.READY;

  const server = createComponentServer({
    bindings,
    implementations: new Map([
      [
        provided.typeName,
        {
          health() {
            return { status, message: "test" };
          },
        },
      ],
    ]),
  });

  try {
    const address = await listenComponentServer(server, { host: "127.0.0.1", port: 0 });
    const target = `dns:///127.0.0.1:${address.port}`;
    const client = createClient(required, createDockerTransport(target));

    const response = await client.health({});
    assert.equal(response.status, HealthStatus.READY);
    assert.equal(response.message, "test");
    assert.equal(await checkComponentHealth({ target }), true);
    status = HealthStatus.UNSPECIFIED;
    assert.equal(await checkComponentHealth({ target }), false);
    status = HealthStatus.NOT_READY;
    assert.equal(await checkComponentHealth({ target }), false);
    await closeComponentServer(server);
    await assert.rejects(checkComponentHealth({ target, timeoutMs: 50 }));
  } finally {
    await closeComponentServer(server);
  }
});
