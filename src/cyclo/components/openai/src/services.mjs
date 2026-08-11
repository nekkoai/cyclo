import { HealthStatus } from "@cyclo/component/contract";

export function createOpenAIServices({ provider, healthTimeoutMs = 1_000 } = {}) {
  if (!provider?.client || typeof provider.callOptions !== "function") {
    throw new TypeError("a Provider binding is required");
  }
  if (!Number.isSafeInteger(healthTimeoutMs) || healthTimeoutMs <= 0) {
    throw new TypeError("healthTimeoutMs must be a positive integer");
  }

  return Object.freeze({
    component: Object.freeze({
      async health(_request, context) {
        try {
          await provider.client.listModels(
            {},
            provider.callOptions(context.signal, healthTimeoutMs),
          );
          return { status: HealthStatus.READY, message: "ready" };
        } catch {
          return {
            status: HealthStatus.NOT_READY,
            message: "provider unavailable",
          };
        }
      },
    }),
  });
}
