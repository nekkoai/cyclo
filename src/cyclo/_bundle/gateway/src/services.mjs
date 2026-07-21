import { Code, ConnectError } from "@connectrpc/connect";
import { getApiProvider } from "@earendil-works/pi-ai/compat";
import { getOAuthProvider } from "@earendil-works/pi-ai/oauth";
import {
  getBuiltinModels,
  getBuiltinProviders,
} from "@earendil-works/pi-ai/providers/all";
import { HealthStatus } from "@cyclo/component/contract";
import { validateInferStream } from "@cyclo/provider/protocol";

import { createUsageAudit, usageRecord } from "./audit.mjs";
import { buildCatalogue } from "./catalogue.mjs";
import { createCredentialResolver } from "./credentials.mjs";
import { createPiAdapter } from "./pi-adapter.mjs";
import { prepareInference } from "./request.mjs";

const DEFAULTS = Object.freeze({
  authPath: "/var/lib/cyclo-gateway/auth.json",
  modelsPath: "/etc/cyclo-gateway/models.json",
  usagePath: "/var/lib/cyclo-gateway/usage.jsonl",
});

export function createGatewayServices(options = {}) {
  const env = options.env ?? process.env;
  const paths = {
    authPath: env.CYCLO_GATEWAY_AUTH_JSON ?? DEFAULTS.authPath,
    modelsPath: env.CYCLO_GATEWAY_MODELS_JSON ?? DEFAULTS.modelsPath,
    usagePath: env.CYCLO_GATEWAY_USAGE_JSONL ?? DEFAULTS.usagePath,
  };
  const catalogue = options.catalogue ?? buildCatalogue({
    authPath: paths.authPath,
    modelsPath: paths.modelsPath,
    getBuiltinProviders,
    getBuiltinModels,
    getApiProvider,
    getOAuthProvider,
  });
  const credentials = options.credentials ?? createCredentialResolver({
    authPath: paths.authPath,
    getOAuthProvider,
  });
  const backend = options.backend ?? createPiAdapter();
  const audit = options.audit ?? createUsageAudit(paths.usagePath);

  return Object.freeze({
    component: Object.freeze({
      health() {
        try {
          credentials.check?.(Object.values(catalogue.routes));
          audit.check?.();
          return { status: HealthStatus.READY, message: "ready" };
        } catch {
          return {
            status: HealthStatus.NOT_READY,
            message: "gateway configuration unavailable",
          };
        }
      },
    }),
    provider: Object.freeze({
      listModels() {
        return { models: catalogue.models };
      },

      async *infer(request, context) {
        const started = Date.now();
        const route = catalogue.routes[request.model];
        if (!route) throw new ConnectError("unknown model", Code.NotFound);
        const prepared = prepareInference(request, route);

        let credential;
        try {
          credential = await credentials.resolve(route);
        } catch {
          await record(audit, usageRecord({
            model: request.model,
            started,
            outcome: "credential_unavailable",
          }));
          throw new ConnectError("gateway credential unavailable", Code.Unavailable);
        }

        let usage;
        let terminal;
        let failure;
        let auditAttempted = false;
        const writeOutcome = async (outcome) => {
          auditAttempted = true;
          await record(audit, usageRecord({
            model: request.model,
            started,
            outcome,
            usage,
          }));
        };
        try {
          try {
            const dispatchRoute = credential.effectiveModel
              ? { ...route, rawModel: credential.effectiveModel }
              : route;
            const responses = backend.infer(
              dispatchRoute,
              prepared,
              credential,
              context.signal,
            );
            for await (const response of validateInferStream(responses, { model: request.model })) {
              if (response.event.case === "finished") {
                usage = response.event.value.usage;
                terminal = response;
              } else {
                yield response;
              }
            }
          } catch (error) {
            failure = error;
          }

          await writeOutcome(failure instanceof ConnectError
            ? `rpc_${failure.code}`
            : failure
              ? "internal"
              : "ok");
          if (failure instanceof ConnectError) throw failure;
          if (failure) throw new ConnectError("gateway inference failed", Code.Internal);
          yield terminal;
        } finally {
          if (!auditAttempted) {
            await writeOutcome(context.signal?.aborted ? `rpc_${Code.Canceled}` : "client_abandoned");
          }
        }
      },
    }),
  });
}

async function record(audit, value) {
  try {
    await audit.record(value);
  } catch {
    throw new ConnectError("gateway usage audit unavailable", Code.Internal);
  }
}
