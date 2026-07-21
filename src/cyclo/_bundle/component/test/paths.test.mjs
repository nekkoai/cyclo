import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_COMPONENT_SOCKET,
  componentSocketPath,
  requirementSocketPath,
} from "../src/paths.mjs";
import { createUnixTransport } from "../src/transport.mjs";

test("component binding paths have stable defaults and explicit overrides", () => {
  assert.equal(DEFAULT_COMPONENT_SOCKET, "/run/cyclo/component.sock");
  assert.equal(componentSocketPath({}), DEFAULT_COMPONENT_SOCKET);
  assert.equal(
    requirementSocketPath("upstream", {}),
    "/run/cyclo/requirements/upstream/component.sock",
  );
  const env = {
    CYCLO_COMPONENT_SOCKET: " /tmp/own.sock ",
    CYCLO_REQUIRE_MODEL_POOL_SOCKET: " /tmp/pool.sock ",
  };
  assert.equal(componentSocketPath(env), "/tmp/own.sock");
  assert.equal(requirementSocketPath("model-pool", env), "/tmp/pool.sock");
});

test("component binding names and paths fail closed", () => {
  for (const name of ["", "UPSTREAM", "../upstream", "up_stream"]) {
    assert.throws(() => requirementSocketPath(name, {}), /name is invalid/u);
  }
  for (const value of ["", "   ", "relative.sock"]) {
    assert.throws(
      () => componentSocketPath({ CYCLO_COMPONENT_SOCKET: value }),
      /non-empty|absolute/u,
    );
  }
  assert.throws(() => createUnixTransport("relative.sock"), /absolute/u);
});
