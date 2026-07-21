import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { Component } from "@cyclo/component/contract";
import { registerProvides, resolveBindings } from "@cyclo/component/bindings";
import { parseDeclaration } from "@cyclo/component/declaration";
import { servicesFromDescriptorSet } from "@cyclo/component/schema";
import { Provider } from "@cyclo/provider/contract";

const schemaUrl = new URL("../gen/schema.json", import.meta.url);

test("exports the versioned Provider service contract", () => {
  assert.equal(Provider.typeName, "cyclo.provider.v1.Provider");
  assert.deepEqual(
    Provider.methods.map(({ name, methodKind }) => ({ name, methodKind })),
    [
      { name: "ListModels", methodKind: "unary" },
      { name: "Infer", methodKind: "server_streaming" },
    ],
  );
});

test("generated schema contains the Provider interface", async () => {
  const schema = JSON.parse(await readFile(schemaUrl, "utf8"));
  assert.deepEqual(
    servicesFromDescriptorSet(schema),
    new Set([Provider.typeName]),
  );
});

test("a pass-through provides Provider and requires one named upstream", () => {
  const declaration = parseDeclaration(`
    component passthrough
    provide cyclo.component.v1.Component
    provide cyclo.provider.v1.Provider
    require upstream cyclo.provider.v1.Provider
  `);
  const bindings = resolveBindings(declaration, [Component, Provider]);

  assert.equal(bindings.provides.get(Provider.typeName), Provider);
  assert.equal(bindings.requires.get("upstream"), Provider);
});

test("a multiplexer has distinct inputs of the same Provider interface", () => {
  const declaration = parseDeclaration(`
    component multiplexer
    provide cyclo.component.v1.Component
    provide cyclo.provider.v1.Provider
    require primary cyclo.provider.v1.Provider
    require secondary cyclo.provider.v1.Provider
  `);
  const bindings = resolveBindings(declaration, [Component, Provider]);

  assert.equal(bindings.requires.size, 2);
  assert.equal(bindings.requires.get("primary"), Provider);
  assert.equal(bindings.requires.get("secondary"), Provider);
});

test("Provider implementations must implement both RPCs before registration", () => {
  const declaration = parseDeclaration(`
    component incomplete-provider
    provide cyclo.component.v1.Component
    provide cyclo.provider.v1.Provider
  `);
  const bindings = resolveBindings(declaration, [Component, Provider]);
  let registrations = 0;
  const router = { service() { registrations += 1; } };

  assert.throws(
    () =>
      registerProvides(
        router,
        bindings,
        new Map([
          [Component.typeName, { health() {} }],
          [Provider.typeName, { listModels() { return { models: [] }; } }],
        ]),
      ),
    /missing implementation for cyclo\.provider\.v1\.Provider\/Infer/u,
  );
  assert.equal(registrations, 0);
});
