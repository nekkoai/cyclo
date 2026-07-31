import assert from "node:assert/strict";
import test from "node:test";

import {
  LOCAL_COMPONENT_TARGET,
  dcompLink,
} from "../src/links.mjs";
import { createDockerTransport, parseDockerTarget } from "../src/transport.mjs";

test("component targets use the fixed listener and DComp link variables", () => {
  assert.equal(LOCAL_COMPONENT_TARGET, "dns:///127.0.0.1:50051");
  assert.equal(
    dcompLink("upstream", { DCOMP_LINK_UPSTREAM: "dns:///source:50051" }),
    "dns:///source:50051",
  );
  assert.equal(
    dcompLink("model-pool", { DCOMP_LINK_MODEL_POOL: "dns:///pool:50051" }),
    "dns:///pool:50051",
  );
});

test("component binding names and targets fail closed", () => {
  for (const name of ["", "UPSTREAM", "../upstream", "up_stream"]) {
    assert.throws(() => dcompLink(name, {}), /input name is invalid/u);
  }
  assert.throws(() => dcompLink("upstream", {}), /DCOMP_LINK_UPSTREAM is required/u);
  for (const value of [
    "",
    " dns:///upstream:50051",
    "dns://upstream:50051",
    "dns:///upstream",
    "dns:///upstream:0",
    "dns:///upstream:65536",
    "dns:///user@upstream:50051",
    "https://upstream:50051",
  ]) {
    assert.throws(
      () => createDockerTransport(value),
      /DComp target/u,
    );
  }
  assert.deepEqual(parseDockerTarget("dns:///provider-1:50051"), {
    host: "provider-1",
    port: 50051,
  });
});
