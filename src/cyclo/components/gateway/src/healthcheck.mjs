import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { checkComponentHealth } from "@cyclo/component/health";

export async function checkGatewayHealth({
  target,
  timeoutMs = 1_000,
} = {}) {
  return checkComponentHealth({ target, timeoutMs });
}

function isMain() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (isMain()) {
  checkGatewayHealth()
    .then((ready) => {
      process.exitCode = ready ? 0 : 1;
    })
    .catch(() => {
      process.exitCode = 1;
    });
}
