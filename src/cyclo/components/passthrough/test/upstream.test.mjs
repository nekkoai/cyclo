import assert from "node:assert/strict";
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

test("createUpstreamBinding uses only its DComp link and never carries caller credentials", async (t) => {
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
  const address = await listenComponentServer(server, {
    host: "127.0.0.1",
    port: 0,
  });

  const binding = await createUpstreamBinding({
    env: {
      DCOMP_LINK_UPSTREAM: `dns:///127.0.0.1:${address.port}`,
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

test("createUpstreamBinding requires a canonical DComp target", () => {
  assert.throws(
    () => createUpstreamBinding({
      env: {
        DCOMP_LINK_UPSTREAM: "upstream:50051",
      },
    }),
    /dns:\/\/\/host:port/u,
  );
  assert.throws(
    () => createUpstreamBinding({ env: {} }),
    /DCOMP_LINK_UPSTREAM is required/u,
  );
});
