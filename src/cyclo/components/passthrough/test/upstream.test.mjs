import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { resolveBindings } from "@cyclo/component/bindings";
import { Component, HealthStatus } from "@cyclo/component/contract";
import { parseDeclaration } from "@cyclo/component/declaration";
import {
  closeComponentServer,
  createComponentServer,
  listenComponentServer,
} from "@cyclo/component/server";
import { Provider } from "@cyclo/provider/contract";

import { createUpstreamBinding } from "../src/upstream.mjs";

test("createUpstreamBinding uses only its UDS and never carries caller credentials", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "cyclo-passthrough-upstream-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const socketPath = join(directory, "custom-upstream.sock");

  let requestHeaders;
  const bindings = resolveBindings(
    parseDeclaration(`
      component test-upstream
      provide cyclo.component.v1.Component
      provide cyclo.provider.v1.Provider
    `),
    [Component, Provider],
  );
  const server = createComponentServer({
    bindings,
    implementations: new Map([
      [Component.typeName, {
        health() {
          return { status: HealthStatus.READY, message: "ready" };
        },
      }],
      [Provider.typeName, {
        listModels(_request, context) {
          requestHeaders = new Headers(context.requestHeader);
          return { models: [] };
        },
        async *infer() {},
      }],
    ]),
  });
  t.after(() => closeComponentServer(server));
  await listenComponentServer(server, { socketPath });

  const binding = await createUpstreamBinding({
    env: {
      CYCLO_REQUIRE_UPSTREAM_SOCKET: `  ${socketPath}  `,
    },
  });
  const controller = new AbortController();
  const poisoned = binding.callOptions(controller.signal, 500);
  poisoned.headers = new Headers({
    authorization: "Bearer caller-secret",
    "x-api-key": "caller-secret",
    cookie: "session=caller-secret",
  });

  const options = binding.callOptions(controller.signal, 500);
  assert.equal(options.signal, controller.signal);
  assert.equal(options.timeoutMs, 500);
  assert.equal(Object.hasOwn(options, "headers"), false);
  await binding.client.listModels({}, options);

  for (const name of ["authorization", "x-api-key", "cookie"]) {
    assert.equal(requestHeaders.get(name), null);
  }
  assert.equal([...requestHeaders.values()].some((value) => value.includes("caller-secret")), false);
});

test("createUpstreamBinding rejects invalid socket overrides before constructing a client", () => {
  assert.throws(
    () => createUpstreamBinding({
      env: {
        CYCLO_REQUIRE_UPSTREAM_SOCKET: "relative.sock",
      },
    }),
    /must be absolute/u,
  );
  assert.throws(
    () => createUpstreamBinding({
      env: {
        CYCLO_REQUIRE_UPSTREAM_SOCKET: "   ",
      },
    }),
    /must be non-empty/u,
  );
});
