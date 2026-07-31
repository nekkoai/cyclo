import { Code, ConnectError } from "@connectrpc/connect";
import { getApiProvider } from "@earendil-works/pi-ai/compat";
import { HealthStatus } from "@cyclo/component/contract";

import { createUsageAudit, usageRecord } from "./audit.mjs";
import { buildCatalogue } from "./catalogue.mjs";
import { createCredentialResolver } from "./credentials.mjs";
import { createPiAdapter } from "./pi-adapter.mjs";

const DEFAULTS = Object.freeze({
  authPath: "/var/lib/cyclo-gateway/auth.json",
  modelsPath: "/var/lib/cyclo-gateway/models.json",
  usagePath: "/var/lib/cyclo-gateway/usage.jsonl",
});

export function createGatewayServices(options = {}) {
  const env = options.env ?? process.env;
  const paths = {
    authPath: env.CYCLO_GATEWAY_AUTH_JSON ?? DEFAULTS.authPath,
    modelsPath: env.CYCLO_GATEWAY_MODELS_JSON ?? DEFAULTS.modelsPath,
    usagePath: env.CYCLO_GATEWAY_USAGE_JSONL ?? DEFAULTS.usagePath,
  };
  const loadCatalogue = options.loadCatalogue ?? (() => buildCatalogue({
    authPath: paths.authPath,
    modelsPath: paths.modelsPath,
    getApiProvider,
  }));
  if (typeof loadCatalogue !== "function") {
    throw new TypeError("loadCatalogue must be a function");
  }
  const catalogue = options.catalogue ?? loadCatalogue();
  const loadedCatalogueSignature = catalogueSignature(catalogue);
  const warn = options.warn ?? console.warn;
  if (typeof warn !== "function") throw new TypeError("warn must be a function");
  for (const diagnostic of catalogue.diagnostics ?? []) {
    const route = safeDiagnostic(
      diagnostic.model
        ? `${diagnostic.account}/${diagnostic.model}`
        : diagnostic.account,
      1_024,
      "unknown",
    );
    warn(
      `Cyclo gateway excluded unusable catalogue entry ${route}: `
      + safeDiagnostic(
        diagnostic.message,
        512,
        "invalid catalogue entry",
      ),
    );
  }
  const credentials = options.credentials ?? createCredentialResolver({
    authPath: paths.authPath,
  });
  const backend = options.backend ?? createPiAdapter();
  const audit = options.audit ?? createUsageAudit(paths.usagePath);

  return Object.freeze({
    component: Object.freeze({
      health() {
        try {
          if (
            options.catalogue === undefined
            && catalogueSignature(loadCatalogue()) !== loadedCatalogueSignature
          ) {
            throw new Error("gateway catalogue changed");
          }
          credentials.check?.(Object.values(catalogue.routes));
          audit.check?.();
          return {
            status: HealthStatus.READY,
            message: "ready",
          };
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
        let failure;
        let pendingResponse;
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
              request.payload,
              credential,
              context.signal,
            );
            for await (const response of responses) {
              if (response.usage !== undefined) usage = response.usage;
              if (pendingResponse !== undefined) {
                yield { payload: pendingResponse.payload };
              }
              pendingResponse = response;
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
          if (pendingResponse !== undefined) {
            yield { payload: pendingResponse.payload };
          }
        } finally {
          if (!auditAttempted) {
            await writeOutcome(context.signal?.aborted ? `rpc_${Code.Canceled}` : "client_abandoned");
          }
        }
      },
    }),
  });
}

function catalogueSignature(catalogue) {
  const routes = Object.keys(catalogue.routes ?? {})
    .sort()
    .map((id) => {
      const {
        apiProvider: _apiProvider,
        publicModel: _publicModel,
        ...route
      } = catalogue.routes[id];
      return [id, route];
    });
  return JSON.stringify(
    {
      models: catalogue.models,
      routes,
      diagnostics: catalogue.diagnostics ?? [],
    },
    (_key, value) => typeof value === "bigint" ? value.toString() : value,
  );
}

async function record(audit, value) {
  try {
    await audit.record(value);
  } catch {
    throw new ConnectError("gateway usage audit unavailable", Code.Internal);
  }
}

function safeDiagnostic(value, limit, fallback) {
  const clean = String(value)
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, limit);
  return clean || fallback;
}
