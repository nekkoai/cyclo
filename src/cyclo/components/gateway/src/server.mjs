import { readFile } from "node:fs/promises";
import { resolveBindings } from "@cyclo/component/bindings";
import { Component } from "@cyclo/component/contract";
import { parseDeclaration } from "@cyclo/component/declaration";
import { createComponentServer } from "@cyclo/component/server";
import { Provider } from "@cyclo/provider/contract";

const defaultComponentConf = new URL("../component.conf", import.meta.url);

export async function createGatewayServer({
  services,
  shutdownSignal,
  componentConf = defaultComponentConf,
} = {}) {
  const declaration = parseDeclaration(await readFile(componentConf, "utf8"), {
    source: componentConf instanceof URL ? componentConf.pathname : String(componentConf),
  });
  const bindings = resolveBindings(declaration, [Component, Provider]);

  if (bindings.requires.size !== 0) {
    throw new TypeError("the gateway component must not require another component");
  }

  const implementations = new Map([
    [Component.typeName, services?.component],
    [Provider.typeName, services?.provider],
  ]);

  return createComponentServer({ bindings, implementations, shutdownSignal });
}
