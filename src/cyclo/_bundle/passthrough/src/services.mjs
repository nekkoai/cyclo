import { HealthStatus } from "@cyclo/component/contract";

export function createPassthroughServices({ upstream, healthTimeoutMs = 1_000 } = {}) {
  if (!upstream?.client || typeof upstream.callOptions !== "function") {
    throw new TypeError("an upstream Provider binding is required");
  }
  if (!Number.isSafeInteger(healthTimeoutMs) || healthTimeoutMs <= 0) {
    throw new TypeError("healthTimeoutMs must be a positive integer");
  }

  return Object.freeze({
    component: Object.freeze({
      async health(_request, context) {
        try {
          await upstream.client.listModels(
            {},
            upstream.callOptions(context.signal, healthTimeoutMs),
          );
          return { status: HealthStatus.READY, message: "ready" };
        } catch {
          return {
            status: HealthStatus.NOT_READY,
            message: "upstream provider unavailable",
          };
        }
      },
    }),
    provider: Object.freeze({
      listModels(request, context) {
        return upstream.client.listModels(
          request,
          upstream.callOptions(context.signal),
        );
      },

      async *infer(request, context) {
        yield* upstream.client.infer(
          request,
          upstream.callOptions(context.signal),
        );
      },
    }),
  });
}
