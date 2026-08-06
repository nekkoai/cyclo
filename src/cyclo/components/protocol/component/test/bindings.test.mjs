import assert from "node:assert/strict";
import test from "node:test";

import { Component } from "../gen/cyclo/component/v1/component_pb.js";
import { registerProvides, resolveBindings } from "../src/bindings.mjs";
import { parseDeclaration } from "../src/declaration.mjs";

test("resolves provided handlers and required clients from one descriptor", () => {
  const declaration = parseDeclaration(`
    component health-proxy
    provide cyclo.component.v1.Component
    require upstream cyclo.component.v1.Component
  `);

  const bindings = resolveBindings(declaration, [Component]);

  assert.equal(bindings.provides.get(Component.typeName), Component);
  assert.equal(bindings.requires.get("upstream"), Component);
});

test("fails when generated descriptors do not satisfy the declaration", () => {
  const declaration = parseDeclaration(`
    component incomplete
    provide cyclo.component.v1.Component
    require models cyclo.models.v1.Catalog
  `);

  assert.throws(
    () => resolveBindings(declaration, [Component]),
    /unknown required interface cyclo\.models\.v1\.Catalog/u,
  );
});

test("a provided interface must implement every RPC", () => {
  const declaration = parseDeclaration(`
    component incomplete
    provide cyclo.component.v1.Component
  `);
  const bindings = resolveBindings(declaration, [Component]);
  const router = { service() { assert.fail("incomplete service was registered"); } };

  assert.throws(
    () => registerProvides(router, bindings, new Map([[Component.typeName, {}]])),
    /missing implementation for cyclo\.component\.v1\.Component\/Health/u,
  );
});

test("all provided interfaces are validated before any is registered", () => {
  const Other = {
    typeName: "example.v1.Other",
    methods: [{ localName: "run", name: "Run" }],
  };
  const declaration = parseDeclaration(`
    component atomic
    provide cyclo.component.v1.Component
    provide example.v1.Other
  `);
  const bindings = resolveBindings(declaration, [Component, Other]);
  let registrations = 0;
  const router = { service() { registrations += 1; } };

  assert.throws(
    () =>
      registerProvides(
        router,
        bindings,
        new Map([
          [Component.typeName, { health() {} }],
          [Other.typeName, {}],
        ]),
      ),
    /missing implementation for example\.v1\.Other\/Run/u,
  );
  assert.equal(registrations, 0);
});
