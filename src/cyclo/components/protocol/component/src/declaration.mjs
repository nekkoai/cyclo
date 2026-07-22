const NAME = /^[a-z][a-z0-9-]*$/;
const SERVICE = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/;

export const COMPONENT_INTERFACE = "cyclo.component.v1.Component";

export class DeclarationError extends Error {
  constructor(source, line, message) {
    super(`${source}:${line}: ${message}`);
    this.name = "DeclarationError";
  }
}

export function parseDeclaration(text, { source = "component.conf" } = {}) {
  const declaration = { name: undefined, provides: [], requires: [] };
  const provided = new Set();
  const required = new Set();

  for (const [index, raw] of text.split(/\r?\n/u).entries()) {
    const line = index + 1;
    const content = raw.replace(/\s+#.*$/u, "").trim();
    if (!content || content.startsWith("#")) continue;

    const fields = content.split(/\s+/u);
    switch (fields[0]) {
      case "component": {
        if (fields.length !== 2) {
          throw new DeclarationError(source, line, "expected: component NAME");
        }
        if (declaration.name !== undefined) {
          throw new DeclarationError(source, line, "duplicate component declaration");
        }
        checkName(fields[1], source, line, "component name");
        declaration.name = fields[1];
        break;
      }
      case "provide": {
        if (fields.length !== 2) {
          throw new DeclarationError(source, line, "expected: provide SERVICE");
        }
        checkService(fields[1], source, line);
        if (provided.has(fields[1])) {
          throw new DeclarationError(source, line, `duplicate provided interface ${fields[1]}`);
        }
        provided.add(fields[1]);
        declaration.provides.push(fields[1]);
        break;
      }
      case "require": {
        if (fields.length !== 3) {
          throw new DeclarationError(source, line, "expected: require NAME SERVICE");
        }
        checkName(fields[1], source, line, "requirement name");
        checkService(fields[2], source, line);
        if (required.has(fields[1])) {
          throw new DeclarationError(source, line, `duplicate requirement name ${fields[1]}`);
        }
        required.add(fields[1]);
        declaration.requires.push({ name: fields[1], service: fields[2] });
        break;
      }
      default:
        throw new DeclarationError(source, line, `unknown directive ${fields[0]}`);
    }
  }

  if (declaration.name === undefined) {
    throw new DeclarationError(source, 1, "missing component declaration");
  }
  if (!provided.has(COMPONENT_INTERFACE)) {
    throw new DeclarationError(
      source,
      1,
      `every component must provide ${COMPONENT_INTERFACE}`,
    );
  }
  return declaration;
}

export function validateInterfaces(declaration, services, { source = "component.conf" } = {}) {
  const known = services instanceof Set ? services : new Set(services);
  for (const service of declaration.provides) {
    if (!known.has(service)) {
      throw new DeclarationError(source, 1, `unknown provided interface ${service}`);
    }
  }
  for (const requirement of declaration.requires) {
    if (!known.has(requirement.service)) {
      throw new DeclarationError(source, 1, `unknown required interface ${requirement.service}`);
    }
  }
  return declaration;
}

function checkName(value, source, line, kind) {
  if (!NAME.test(value)) {
    throw new DeclarationError(source, line, `invalid ${kind} ${JSON.stringify(value)}`);
  }
}

function checkService(value, source, line) {
  if (!SERVICE.test(value)) {
    throw new DeclarationError(source, line, `invalid interface name ${JSON.stringify(value)}`);
  }
}
