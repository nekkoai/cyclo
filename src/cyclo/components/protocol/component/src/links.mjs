const INPUT_NAME = /^[a-z][a-z0-9-]*$/u;

export const LOCAL_COMPONENT_TARGET = "dns:///127.0.0.1:50051";

export function dcompLink(name, env = process.env) {
  if (typeof name !== "string" || !INPUT_NAME.test(name)) {
    throw new TypeError("input name is invalid");
  }
  const variable = `DCOMP_LINK_${name.replaceAll("-", "_").toUpperCase()}`;
  const target = env[variable];
  if (typeof target !== "string" || !target) {
    throw new TypeError(`${variable} is required`);
  }
  return target;
}
