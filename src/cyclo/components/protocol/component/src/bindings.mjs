import { validateInterfaces } from "./declaration.mjs";

// Resolve a declaration against generated Connect service descriptors.
// The returned descriptors are used directly by router.service() and
// createClient(); Connect deliberately uses the same contract on both sides.
export function resolveBindings(declaration, descriptors, options = {}) {
  const services = new Map();
  for (const descriptor of descriptors) {
    if (!descriptor || typeof descriptor.typeName !== "string") {
      throw new TypeError("expected a generated Connect service descriptor");
    }
    if (services.has(descriptor.typeName)) {
      throw new TypeError(`duplicate service descriptor ${descriptor.typeName}`);
    }
    services.set(descriptor.typeName, descriptor);
  }

  validateInterfaces(declaration, services.keys(), options);

  return {
    provides: new Map(
      declaration.provides.map((service) => [service, services.get(service)]),
    ),
    requires: new Map(
      declaration.requires.map(({ name, service }) => [name, services.get(service)]),
    ),
  };
}

// A declared interface is a complete obligation. Connect itself permits
// partial implementations, so reject omissions before opening a listener.
export function registerProvides(router, bindings, implementations) {
  for (const service of implementations.keys()) {
    if (!bindings.provides.has(service)) {
      throw new TypeError(`implementation supplied for undeclared interface ${service}`);
    }
  }

  for (const [service, descriptor] of bindings.provides) {
    const implementation = implementations.get(service);
    if (!implementation || typeof implementation !== "object") {
      throw new TypeError(`missing implementation for provided interface ${service}`);
    }
    for (const method of descriptor.methods) {
      if (typeof implementation[method.localName] !== "function") {
        throw new TypeError(`missing implementation for ${service}/${method.name}`);
      }
    }
  }

  for (const [service, descriptor] of bindings.provides) {
    const implementation = implementations.get(service);
    router.service(descriptor, implementation);
  }
  return router;
}
