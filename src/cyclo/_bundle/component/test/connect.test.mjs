import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
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
import { createUnixTransport } from "../src/transport.mjs";

test("generated handler and client communicate over a Unix socket", async () => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-component-"));
  const socketPath = join(directory, "component.sock");
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
    await listenComponentServer(server, { socketPath });
    const client = createClient(required, createUnixTransport(socketPath));

    const response = await client.health({});
    assert.equal(response.status, HealthStatus.READY);
    assert.equal(response.message, "test");
    assert.equal(await checkComponentHealth({ socketPath }), true);
    status = HealthStatus.UNSPECIFIED;
    assert.equal(await checkComponentHealth({ socketPath }), false);
    status = HealthStatus.NOT_READY;
    assert.equal(await checkComponentHealth({ socketPath }), false);
  } finally {
    await closeComponentServer(server);
  }
  try {
    await assert.rejects(checkComponentHealth({ socketPath, timeoutMs: 50 }));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
