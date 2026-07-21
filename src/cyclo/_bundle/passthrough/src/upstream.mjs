import { createClient } from "@connectrpc/connect";
import { requirementSocketPath } from "@cyclo/component/paths";
import { createUnixTransport } from "@cyclo/component/transport";
import { Provider } from "@cyclo/provider/contract";

export function createUpstreamBinding({ env = process.env } = {}) {
  const client = createClient(
    Provider,
    createUnixTransport(requirementSocketPath("upstream", env)),
  );

  return Object.freeze({
    client,
    callOptions(signal, timeoutMs) {
      const options = { signal };
      if (timeoutMs !== undefined) options.timeoutMs = timeoutMs;
      return options;
    },
  });
}
