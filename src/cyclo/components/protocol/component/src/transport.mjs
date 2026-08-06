import { createConnectTransport } from "@connectrpc/connect-node";

const DOCKER_TARGET = /^dns:\/\/\/([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?):([0-9]+)$/u;

export function createDockerTransport(target) {
  const { host, port } = parseDockerTarget(target);
  return createConnectTransport({
    baseUrl: `http://${host}:${port}`,
    httpVersion: "1.1",
  });
}

export function parseDockerTarget(target) {
  if (typeof target !== "string" || target.trim() !== target || !target) {
    throw new TypeError("DComp target must be a non-empty canonical string");
  }
  const match = DOCKER_TARGET.exec(target);
  if (!match) {
    throw new TypeError("DComp target must have the form dns:///host:port");
  }
  const port = Number(match[2]);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new TypeError("DComp target port must be between 1 and 65535");
  }
  return Object.freeze({ host: match[1], port });
}
