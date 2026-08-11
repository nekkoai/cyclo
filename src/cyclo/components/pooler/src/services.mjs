import { Code, ConnectError } from "@connectrpc/connect";
import { HealthStatus } from "@cyclo/component/contract";
import {
  createResourceExhaustedError,
  resourceExhaustedRetryAt,
} from "@cyclo/provider/errors";

import { composeCatalog } from "./catalog.mjs";
import { outputModelId, parseComponentName } from "./config.mjs";
import { PoolScheduler } from "./pool.mjs";

export function createPoolerServices({
  upstream,
  config,
  componentName,
  healthTimeoutMs = 1_000,
  logError = (message) => console.error(message),
} = {}) {
  if (!upstream?.client || typeof upstream.callOptions !== "function") {
    throw new TypeError("an upstream Provider binding is required");
  }
  if (!Number.isSafeInteger(healthTimeoutMs) || healthTimeoutMs <= 0) {
    throw new TypeError("healthTimeoutMs must be a positive integer");
  }
  if (typeof logError !== "function") {
    throw new TypeError("logError must be a function");
  }

  const outputPrefix = `${parseComponentName(componentName)}/`;
  const providerWide = config?.memberProviders !== undefined;
  const providerSchedulerKey = Symbol("provider-wide pool");
  const exactOutputId = config?.memberModelIds === undefined
    ? undefined
    : outputModelId(componentName, config.outputModel);
  const initialSchedulerKey = providerWide ? providerSchedulerKey : exactOutputId;
  const initialMemberIds = providerWide
    ? config.memberProviders
    : config?.memberModelIds;
  let schedulerStates = new Map();
  if (initialSchedulerKey !== undefined && initialMemberIds !== undefined) {
    schedulerStates.set(initialSchedulerKey, {
      memberIds: initialMemberIds,
      scheduler: new PoolScheduler(initialMemberIds),
    });
  }
  let catalogue;
  let refreshGeneration = 0;
  let lastFailureSignature;

  function reportCatalogueFailure(error, cause) {
    const signature = `${error.code}:${error.rawMessage}`;
    if (signature === lastFailureSignature) return;
    lastFailureSignature = signature;
    const causeDetail = cause instanceof Error
      ? cause.stack ?? `${cause.name}: ${cause.message}`
      : String(cause);
    try {
      logError(
        `cyclo-pooler ${componentName}: catalogue refresh failed: `
        + `${error.message}\nCaused by:\n${causeDetail}`,
      );
    } catch {
      // A diagnostic sink must never replace the catalogue failure callers need.
    }
  }

  function activateCatalogue(resolved) {
    const nextSchedulerStates = new Map();
    const routes = new Map();
    for (const pool of resolved.pools) {
      const outputId = pool.virtualModel.id;
      const schedulerKey = providerWide ? providerSchedulerKey : outputId;
      const schedulerMemberIds = providerWide
        ? config.memberProviders
        : pool.memberModelIds;
      const previous = nextSchedulerStates.get(schedulerKey)
        ?? schedulerStates.get(schedulerKey);
      const scheduler = previous !== undefined
        && sameMemberIds(previous.memberIds, schedulerMemberIds)
        ? previous.scheduler
        : new PoolScheduler(schedulerMemberIds);
      nextSchedulerStates.set(schedulerKey, {
        memberIds: schedulerMemberIds,
        scheduler,
      });

      const targetModelIds = new Map(pool.members.map((model) => [
        providerWide ? modelProvider(model.id) : model.id,
        model.id,
      ]));
      routes.set(outputId, Object.freeze({
        eligibleMemberIds: Object.freeze([...targetModelIds.keys()]),
        scheduler,
        targetModelIds,
      }));
    }
    return Object.freeze({
      active: Object.freeze({ models: resolved.models, routes }),
      schedulerStates: nextSchedulerStates,
    });
  }

  async function refreshCatalogue(signal, timeoutMs) {
    const generation = ++refreshGeneration;
    let failureContext = {
      code: Code.Unavailable,
      operation: "could not list upstream models",
    };
    try {
      const response = await upstream.client.listModels(
        {},
        upstream.callOptions(signal, timeoutMs),
      );
      failureContext = {
        code: Code.FailedPrecondition,
        operation: "rejected the upstream catalogue",
      };
      const resolved = composeCatalog(response.models, config, componentName);
      failureContext = {
        code: Code.Internal,
        operation: "could not activate the upstream catalogue",
      };
      const activated = activateCatalogue(resolved);
      if (generation === refreshGeneration) {
        schedulerStates = activated.schedulerStates;
        catalogue = activated.active;
        lastFailureSignature = undefined;
      }
      return activated.active;
    } catch (error) {
      const failure = catalogueFailure(error, componentName, failureContext);
      if (generation === refreshGeneration) {
        catalogue = undefined;
        reportCatalogueFailure(failure, error);
      }
      throw failure;
    }
  }

  async function currentCatalogue(signal) {
    return catalogue ?? refreshCatalogue(signal);
  }

  return Object.freeze({
    component: Object.freeze({
      async health(_request, context) {
        try {
          await refreshCatalogue(context.signal, healthTimeoutMs);
          return { status: HealthStatus.READY, message: "ready" };
        } catch (error) {
          return {
            status: HealthStatus.NOT_READY,
            message: errorMessage(error),
          };
        }
      },
    }),
    provider: Object.freeze({
      async listModels(_request, context) {
        const resolved = await refreshCatalogue(context.signal);
        return { models: resolved.models };
      },

      async *infer(request, context) {
        let route = catalogue?.routes.get(request.model);
        if (
          route === undefined
          && isOutputCandidate(request.model, exactOutputId, outputPrefix)
        ) {
          route = (await currentCatalogue(context.signal)).routes.get(request.model);
        }
        if (route === undefined) {
          yield* upstream.client.infer(
            request,
            upstream.callOptions(context.signal),
          );
          return;
        }

        const attempt = route.scheduler.begin(route.eligibleMemberIds);
        while (true) {
          const selected = attempt.next();
          if (selected.retryAt !== undefined) {
            throw createResourceExhaustedError(selected.retryAt);
          }
          const targetModelId = route.targetModelIds.get(selected.memberModelId);
          if (targetModelId === undefined) {
            throw new Error("pool selected a member with no model route");
          }

          let responded = false;
          try {
            for await (const response of upstream.client.infer(
              { model: targetModelId, payload: request.payload },
              upstream.callOptions(context.signal),
            )) {
              responded = true;
              yield response;
            }
            return;
          } catch (error) {
            if (responded) throw error;
            const retryAt = resourceExhaustedRetryAt(error);
            if (retryAt === undefined) throw error;
            attempt.markExhausted(retryAt);
          }
        }
      },
    }),
  });
}


function catalogueFailure(error, componentName, { code, operation }) {
  const existing = error instanceof ConnectError ? error : undefined;
  return new ConnectError(
    `pooler ${componentName} ${operation}: ${errorMessage(error)}`,
    existing?.code ?? code,
    existing?.metadata,
    existing?.details,
    error,
  );
}


function errorMessage(error) {
  if (error instanceof ConnectError && error.rawMessage) return error.rawMessage;
  if (error instanceof Error && error.message) return error.message;
  const detail = String(error);
  return detail || "unknown catalogue failure";
}


function sameMemberIds(first, second) {
  return first.length === second.length
    && first.every((id, position) => id === second[position]);
}


function isOutputCandidate(model, exactOutputId, outputPrefix) {
  if (typeof model !== "string") return false;
  return exactOutputId === undefined
    ? model.startsWith(outputPrefix)
    : model === exactOutputId;
}


function modelProvider(modelId) {
  return modelId.slice(0, modelId.indexOf("/"));
}
