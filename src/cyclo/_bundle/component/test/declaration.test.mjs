import assert from "node:assert/strict";
import test from "node:test";

import {
  COMPONENT_INTERFACE,
  DeclarationError,
  parseDeclaration,
  validateInterfaces,
} from "../src/declaration.mjs";
import { servicesFromDescriptorSet } from "../src/schema.mjs";

test("parses provided interfaces and named requirements", () => {
  const declaration = parseDeclaration(`
    # Interface direction is explicit.
    component health-proxy
    provide ${COMPONENT_INTERFACE}
    require upstream ${COMPONENT_INTERFACE}
  `);

  assert.deepEqual(declaration, {
    name: "health-proxy",
    provides: [COMPONENT_INTERFACE],
    requires: [{ name: "upstream", service: COMPONENT_INTERFACE }],
  });
});

test("requires the common component interface", () => {
  assert.throws(
    () => parseDeclaration("component broken\nprovide example.v1.Other\n"),
    (error) =>
      error instanceof DeclarationError &&
      error.message.includes(`must provide ${COMPONENT_INTERFACE}`),
  );
});

test("rejects duplicate requirement names", () => {
  assert.throws(
    () =>
      parseDeclaration(`
        component duplicate
        provide ${COMPONENT_INTERFACE}
        require upstream example.v1.First
        require upstream example.v1.Second
      `),
    /duplicate requirement name upstream/u,
  );
});

test("rejects unknown directives", () => {
  assert.throws(
    () => parseDeclaration(`component bad\nprovide ${COMPONENT_INTERFACE}\nport 8080\n`),
    /unknown directive port/u,
  );
});

test("validates declarations against compiled services", () => {
  const declaration = parseDeclaration(`
    component unknown
    provide ${COMPONENT_INTERFACE}
    require upstream example.v1.Missing
  `);

  assert.throws(
    () => validateInterfaces(declaration, new Set([COMPONENT_INTERFACE])),
    /unknown required interface example\.v1\.Missing/u,
  );
});

test("extracts fully-qualified services from descriptor JSON", () => {
  const services = servicesFromDescriptorSet({
    file: [
      { package: "cyclo.component.v1", service: [{ name: "Component" }] },
      { package: "example.v1", service: [{ name: "Echo" }] },
    ],
  });

  assert.deepEqual(services, new Set([COMPONENT_INTERFACE, "example.v1.Echo"]));
});
