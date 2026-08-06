import { readFile } from "node:fs/promises";

import { resolveBindings } from "@cyclo/component/bindings";
import { Component } from "@cyclo/component/contract";
import { parseDeclaration } from "@cyclo/component/declaration";
import { createComponentServer } from "@cyclo/component/server";
import { Provider } from "@cyclo/provider/contract";

const defaultComponentConf = new URL("../component.conf", import.meta.url);

export async function createPassthroughServer({
  services,
  shutdownSignal,
  componentConf = defaultComponentConf,
} = {}) {
  const declaration = parseDeclaration(await readFile(componentConf, "utf8"), {
    source: componentConf instanceof URL ? componentConf.pathname : String(componentConf),
  });
  const bindings = resolveBindings(declaration, [Component, Provider]);
  const upstream = bindings.requires.get("upstream");
  if (bindings.requires.size !== 1 || upstream?.typeName !== Provider.typeName) {
    throw new TypeError("passthrough must require exactly one upstream Provider");
  }

  return createComponentServer({
    bindings,
    shutdownSignal,
    implementations: new Map([
      [Component.typeName, services?.component],
      [Provider.typeName, services?.provider],
    ]),
  });
}
