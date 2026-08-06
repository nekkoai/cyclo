#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import { parseDeclaration, validateInterfaces } from "./declaration.mjs";
import { servicesFromDescriptorSet } from "./schema.mjs";

const [, , declarationPath, ...schemaPaths] = process.argv;
if (!declarationPath || schemaPaths.length === 0) {
  console.error("usage: node src/check.mjs COMPONENT_CONF SCHEMA_JSON...");
  process.exitCode = 2;
} else {
  try {
    const [text, ...rawSchemas] = await Promise.all([
      readFile(declarationPath, "utf8"),
      ...schemaPaths.map((path) => readFile(path, "utf8")),
    ]);
    const services = new Map();
    for (const [index, rawSchema] of rawSchemas.entries()) {
      for (const service of servicesFromDescriptorSet(JSON.parse(rawSchema))) {
        const previous = services.get(service);
        if (previous !== undefined) {
          throw new Error(
            `duplicate interface ${service} in ${previous} and ${schemaPaths[index]}`,
          );
        }
        services.set(service, schemaPaths[index]);
      }
    }
    const declaration = parseDeclaration(text, { source: declarationPath });
    validateInterfaces(declaration, services.keys(), {
      source: declarationPath,
    });
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
