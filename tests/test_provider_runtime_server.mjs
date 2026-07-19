import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";


function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}


function listenTcp(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
}


function listenUnix(server, path) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(path, () => {
      server.off("error", reject);
      resolve();
    });
  });
}


function listenManagedUnix(listenRuntimeSocket, server, path, mode) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    listenRuntimeSocket(server, path, () => {
      server.off("error", reject);
      resolve();
    }, mode);
  });
}


function close(server) {
  if (!server?.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}


function connectUnix(path) {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ path });
    socket.once("error", reject);
    socket.once("connect", () => {
      socket.off("error", reject);
      resolve(socket);
    });
  });
}


function connectTcp(port) {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    socket.once("error", reject);
    socket.once("connect", () => {
      socket.off("error", reject);
      resolve(socket);
    });
  });
}


function unixRequest(socketPath, path, options = {}) {
  return new Promise((resolve, reject) => {
    const request = httpRequest({
      socketPath,
      path,
      method: options.method ?? "GET",
      headers: options.headers,
      agent: false,
    }, async (response) => {
      try {
        const chunks = [];
        for await (const chunk of response) chunks.push(Buffer.from(chunk));
        const body = Buffer.concat(chunks);
        resolve({
          status: response.statusCode,
          headers: response.headers,
          text: () => body.toString("utf8"),
          json: () => JSON.parse(body.toString("utf8")),
        });
      } catch (error) {
        reject(error);
      }
    });
    request.once("error", reject);
    request.end(options.body);
  });
}


function streamingRequest(url, options = {}) {
  const target = new URL(url);
  let request;
  const response = new Promise((resolve, reject) => {
    request = httpRequest({
      hostname: target.hostname,
      port: target.port,
      path: `${target.pathname}${target.search}`,
      method: options.method ?? "POST",
      headers: options.headers,
      agent: false,
    }, async (incoming) => {
      try {
        const chunks = [];
        for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
        const body = Buffer.concat(chunks);
        resolve({
          status: incoming.statusCode,
          text: () => body.toString("utf8"),
        });
      } catch (error) {
        reject(error);
      }
    });
    request.once("error", reject);
  });
  return { request, response };
}


function streamingUnixRequest(socketPath, path, options = {}) {
  let request;
  const response = new Promise((resolve, reject) => {
    request = httpRequest({
      socketPath,
      path,
      method: options.method ?? "POST",
      headers: options.headers,
      agent: false,
    }, async (incoming) => {
      try {
        const chunks = [];
        for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
        const body = Buffer.concat(chunks);
        resolve({
          status: incoming.statusCode,
          text: () => body.toString("utf8"),
        });
      } catch (error) {
        reject(error);
      }
    });
    request.once("error", reject);
  });
  return { request, response };
}


function replaceFile(path, content) {
  const temporary = `${path}.replacement`;
  writeFileSync(temporary, content);
  renameSync(temporary, path);
}


async function waitFor(predicate, timeoutMs = 1_000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for test state");
    await new Promise((resolve) => setImmediate(resolve));
  }
}


test("standalone runtime uses one cached state and preserves team attribution", async (t) => {
  const temporary = mkdtempSync(join(tmpdir(), "cyclo-provider-runtime-"));
  const state = join(temporary, "state");
  const configDirectory = join(temporary, "etc");
  const hostConfig = join(configDirectory, "host.conf");
  const clients = join(state, "clients.json");
  const expected = join(state, "expected-providers.json");
  const registered = join(state, "registered-providers.json");
  const gatewayTokenFile = join(temporary, "gateway.token");
  const adminTokenFile = join(temporary, "runtime-admin.token");
  const runtimeSocketDirectory = join(temporary, "runtime");
  const runtimeProviderDirectory = join(
    runtimeSocketDirectory,
    hash("fusion").slice(0, 32),
  );
  const runtimeSocket = join(runtimeProviderDirectory, "runtime.sock");
  const otherRuntimeDirectory = join(
    runtimeSocketDirectory,
    hash("other").slice(0, 32),
  );
  const otherRuntimeSocket = join(otherRuntimeDirectory, "runtime.sock");
  const adminSocket = join(runtimeSocketDirectory, "admin.sock");
  const providerRoot = join(temporary, "providers");
  const providerId = "a".repeat(32);
  const providerDirectory = join(providerRoot, providerId);
  const providerSocket = join(providerDirectory, "provider.sock");
  for (const directory of [
    state,
    configDirectory,
    runtimeSocketDirectory,
    runtimeProviderDirectory,
    otherRuntimeDirectory,
    providerDirectory,
  ]) {
    mkdirSync(directory, { recursive: true });
  }

  const gatewayServiceToken = "gateway-service-capability";
  const runtimeAdminToken = "runtime-admin-capability";
  const teamToken = "team-original-capability";
  const limitedTeamToken = "team-virtual-only-capability";
  const foreignTeamToken = "team-bound-to-another-network";
  const unboundTeamToken = "team-without-network-binding";
  const ingressToken = "provider-ingress-capability";
  const upstreamToken = "provider-upstream-capability";
  const generation = "provider-generation-one";
  const configuredHost =
    "provider fusion /host/source/not-mounted upstream/input-model strength=1\n";
  writeFileSync(gatewayTokenFile, `${gatewayServiceToken}\n`);
  writeFileSync(adminTokenFile, `${runtimeAdminToken}\n`);
  writeFileSync(hostConfig, configuredHost);
  writeFileSync(clients, `${JSON.stringify({
    version: 1,
    clients: [
      {
        kind: "client",
        client_id: "team-all",
        team_id: "team-all",
        binding_generation: "team-generation",
        token_sha256: hash(teamToken),
        providers: ["upstream", "fusion"],
        models: ["upstream/input-model", "fusion/fused-model"],
        local_addresses: ["127.0.0.1"],
        enabled: true,
        revoked: false,
        expires_at: null,
      },
      {
        kind: "client",
        client_id: "team-virtual",
        team_id: "team-virtual",
        binding_generation: "team-generation",
        token_sha256: hash(limitedTeamToken),
        providers: ["fusion"],
        models: ["fusion/fused-model"],
        local_addresses: ["127.0.0.1"],
        enabled: true,
        revoked: false,
        expires_at: null,
      },
      {
        kind: "client",
        client_id: "team-foreign-network",
        team_id: "team-foreign-network",
        binding_generation: "team-generation",
        token_sha256: hash(foreignTeamToken),
        providers: ["fusion"],
        models: ["fusion/fused-model"],
        local_addresses: ["127.0.0.2"],
        enabled: true,
        revoked: false,
        expires_at: null,
      },
      {
        kind: "client",
        client_id: "team-unbound",
        team_id: "team-unbound",
        binding_generation: "team-generation",
        token_sha256: hash(unboundTeamToken),
        providers: ["fusion"],
        models: ["fusion/fused-model"],
        enabled: true,
        revoked: false,
        expires_at: null,
      },
      {
        kind: "provider",
        provider_prefix: "fusion",
        client_id: "provider-fusion",
        team_id: "provider:fusion",
        binding_generation: generation,
        token_sha256: hash(upstreamToken),
        providers: ["upstream"],
        models: ["upstream/input-model"],
        enabled: true,
        revoked: false,
        expires_at: null,
      },
    ],
  }, null, 2)}\n`);
  writeFileSync(expected, `${JSON.stringify({
    version: 1,
    providers: [{
      prefix: "fusion",
      generation,
      configuration_sha256: hash(JSON.stringify([
        "fusion",
        "/host/source/not-mounted",
        "upstream/input-model",
        "strength=1",
      ])),
      token_sha256: hash(ingressToken),
      inputs: ["upstream/input-model"],
      socket_path: providerSocket,
    }],
  }, null, 2)}\n`);
  const gatewayRequests = [];
  let gatewayCatalogFetches = 0;
  let nextCatalogGate = null;
  const catalogGateQueue = [];
  let chainGate = null;
  const gatewayCatalog = {
    upstream: {
      api: "openai-responses",
      models: [{ id: "input-model", name: "Concrete input", input: ["text"] }],
    },
  };
  const gateway = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = Buffer.concat(chunks).toString("utf8");
    gatewayRequests.push({
      method: request.method,
      path: request.url,
      authorization: request.headers.authorization,
      context: request.headers["x-cyclo-request-context"],
      body,
    });
    if (request.method === "GET" && request.url === "/providers") {
      gatewayCatalogFetches += 1;
      if (request.headers.authorization !== `Bearer ${gatewayServiceToken}`) {
        response.writeHead(401);
        response.end("unauthorized\n");
        return;
      }
      const gate = catalogGateQueue.shift() ?? nextCatalogGate;
      if (gate) {
        if (gate === nextCatalogGate) nextCatalogGate = null;
        gate.started();
        await gate.wait;
      }
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify(gate?.document ?? gatewayCatalog));
      return;
    }
    if (request.method === "POST" && request.url?.startsWith("/p/upstream/")) {
      if (request.url.includes("admission-chain=one") && chainGate) {
        chainGate.arrivals += 1;
        if (chainGate.arrivals === chainGate.expected) chainGate.started();
        await chainGate.wait;
      }
      response.writeHead(200, { "content-type": "text/plain", "x-physical": "yes" });
      response.write("physical-");
      setImmediate(() => response.end("response"));
      return;
    }
    response.writeHead(404);
    response.end("not found\n");
  });
  const gatewayPort = await listenTcp(gateway);

  process.env.CYCLO_PROVIDER_RUNTIME_STATE = state;
  process.env.CYCLO_HOST_CONFIG = hostConfig;
  process.env.CYCLO_PROVIDER_RUNTIME_CLIENTS = clients;
  process.env.CYCLO_PROVIDER_RUNTIME_EXPECTED = expected;
  process.env.CYCLO_PROVIDER_RUNTIME_REGISTERED = registered;
  process.env.CYCLO_PROVIDER_RUNTIME_ADMIN_TOKEN_FILE = adminTokenFile;
  process.env.CYCLO_GATEWAY_TOKEN_FILE = gatewayTokenFile;
  process.env.CYCLO_GATEWAY_BASE_URL = `http://127.0.0.1:${gatewayPort}`;
  process.env.CYCLO_PROVIDER_RUNTIME_SOCKET_ROOT = runtimeSocketDirectory;
  process.env.CYCLO_PROVIDER_RUNTIME_ADMIN_SOCKET = adminSocket;
  process.env.CYCLO_PROVIDER_SOCKET_ROOT = providerRoot;
  process.env.CYCLO_PROVIDER_RUNTIME_INBOUND_TIMEOUT_MS = "3000";

  const runtimeUrl = new URL(
    "../src/cyclo/provider_runtime_context/server.mjs",
    import.meta.url,
  );
  runtimeUrl.searchParams.set("test", String(Date.now()));
  const {
    TRANSPORT_ADMIN_UDS,
    TRANSPORT_PROVIDER_UDS,
    TRANSPORT_TCP,
    createRuntimeState,
    createRuntimeServer,
    listenRuntimeSocket,
  } = await import(runtimeUrl.href);

  const runtimeState = await createRuntimeState();
  assert.equal(gatewayCatalogFetches, 1, "startup loads the concrete catalog once");
  const syntheticAdmissions = [];
  for (let client = 0; client < 3; client += 1) {
    for (let request = 0; request < 8; request += 1) {
      const admission = runtimeState.acquireRequest({
        kind: "client",
        client_id: `synthetic-${client}`,
        team_id: `synthetic-${client}`,
        binding_generation: "test",
      });
      assert.equal(admission.status, 200);
      syntheticAdmissions.push(admission.release);
    }
  }
  const globalDenial = runtimeState.acquireRequest({
    kind: "client",
    client_id: "synthetic-overflow",
    team_id: "synthetic-overflow",
    binding_generation: "test",
  });
  assert.equal(globalDenial.status, 503);
  assert.equal(globalDenial.release, null);
  for (const release of syntheticAdmissions) release();
  assert.equal(runtimeState.activeRequests, 0);
  const sameTeamReleases = [];
  for (let request = 0; request < 8; request += 1) {
    const admission = runtimeState.acquireRequest({
      kind: "client",
      client_id: "project-a",
      team_id: "shared-team",
      binding_generation: "shared-revision",
    });
    assert.equal(admission.status, 200);
    sameTeamReleases.push(admission.release);
  }
  const siblingProject = runtimeState.acquireRequest({
    kind: "client",
    client_id: "project-b",
    team_id: "shared-team",
    binding_generation: "shared-revision",
  });
  assert.equal(siblingProject.status, 200);
  siblingProject.release();
  for (const release of sameTeamReleases) release();
  const syntheticBuffers = [];
  for (let request = 0; request < 12; request += 1) {
    const admission = runtimeState.acquireBufferedRequest({
      kind: "client",
      client_id: request < 8 ? "buffer-a" : "buffer-b",
      team_id: request < 8 ? "buffer-a" : "buffer-b",
      binding_generation: "test",
    });
    assert.equal(admission.status, 200);
    syntheticBuffers.push(admission.release);
  }
  const bufferDenial = runtimeState.acquireBufferedRequest({
    kind: "client",
    client_id: "buffer-c",
    team_id: "buffer-c",
    binding_generation: "test",
  });
  assert.equal(bufferDenial.status, 503);
  assert.equal(bufferDenial.release, null);
  for (const release of syntheticBuffers) release();
  assert.equal(runtimeState.bufferedRequests, 0);
  const firstRegistrationWrite = runtimeState.beginChangedRegistration(
    "synthetic-first",
    10_000,
  );
  assert.notEqual(firstRegistrationWrite, null);
  firstRegistrationWrite();
  assert.equal(
    runtimeState.beginChangedRegistration("synthetic-second", 10_500),
    null,
    "first registrations for different prefixes share the global fsync budget",
  );
  delete runtimeState.changedRegistrationAt["synthetic-first"];
  runtimeState.changedRegistrationGlobalAt = Number.NEGATIVE_INFINITY;
  const runtimeTcp = createRuntimeServer({ state: runtimeState, transport: TRANSPORT_TCP });
  const timeoutRuntimeTcp = createRuntimeServer({
    state: runtimeState,
    transport: TRANSPORT_TCP,
    upstreamTimeoutMs: 50,
  });
  const runtimeUds = createRuntimeServer({
    state: runtimeState,
    transport: TRANSPORT_PROVIDER_UDS,
    providerPrefix: "fusion",
  });
  const runtimeAdminUds = createRuntimeServer({
    state: runtimeState,
    transport: TRANSPORT_ADMIN_UDS,
  });
  const runtimeOtherUds = createRuntimeServer({
    state: runtimeState,
    transport: TRANSPORT_PROVIDER_UDS,
    providerPrefix: "other",
  });
  const runtimePort = await listenTcp(runtimeTcp);
  const timeoutRuntimePort = await listenTcp(timeoutRuntimeTcp);
  await listenManagedUnix(listenRuntimeSocket, runtimeUds, runtimeSocket);
  await listenManagedUnix(listenRuntimeSocket, runtimeAdminUds, adminSocket, 0o600);
  assert.equal(statSync(adminSocket).mode & 0o777, 0o600);
  await listenManagedUnix(listenRuntimeSocket, runtimeOtherUds, otherRuntimeSocket);
  const runtimeBase = `http://127.0.0.1:${runtimePort}`;
  const timeoutRuntimeBase = `http://127.0.0.1:${timeoutRuntimePort}`;
  let provider;
  t.after(async () => {
    await close(provider);
    await close(timeoutRuntimeTcp);
    await close(runtimeTcp);
    await close(runtimeUds);
    await close(runtimeAdminUds);
    await close(runtimeOtherUds);
    await close(gateway);
    rmSync(temporary, { recursive: true, force: true });
  });

  let response = await fetch(`${runtimeBase}/health`);
  assert.equal(response.status, 200);
  const runtimeBootId = response.headers.get("x-cyclo-runtime-boot-id");
  assert.match(runtimeBootId, /^[a-f0-9]{32}$/u);
  assert.equal(await response.text(), "ok\n");

  // Startup has a configured but not-yet-registered component. Its concrete
  // input remains available without probing an absent component endpoint.
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), gatewayCatalog);

  response = await fetch(`${runtimeBase}/p/upstream/v1/responses?direct=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "input-model", input: "direct" }),
  });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "physical-response");
  assert.equal(gatewayRequests.at(-1).authorization, `Bearer ${teamToken}`);
  assert.equal(gatewayRequests.at(-1).context, undefined);

  // Authentication and body buffering are separated by an epoch barrier. A
  // capability reload while a slow body is arriving must never dispatch under
  // the stale principal.
  const epochBody = Buffer.from(JSON.stringify({
    model: "input-model",
    input: "policy-race",
  }));
  const epochRequest = streamingRequest(
    `${runtimeBase}/p/upstream/v1/responses?epoch-race=one`,
    {
      headers: {
        authorization: `Bearer ${teamToken}`,
        "content-type": "application/json",
        "content-length": String(epochBody.length),
      },
    },
  );
  epochRequest.request.write(epochBody.subarray(0, 8));
  await waitFor(() => runtimeState.activeRequests === 1);
  const gatewayPostsBeforeEpochReload = gatewayRequests.filter(
    (item) => item.method === "POST",
  ).length;
  let controlResponse = await unixRequest(adminSocket, "/_cyclo/v1/control/reload", {
    method: "POST",
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(controlResponse.status, 204);
  epochRequest.request.end(epochBody.subarray(8));
  const epochResponse = await epochRequest.response;
  assert.equal(epochResponse.status, 503);
  assert.equal(epochResponse.text(), "provider runtime policy changed; retry request\n");
  assert.equal(
    gatewayRequests.filter((item) => item.method === "POST").length,
    gatewayPostsBeforeEpochReload,
  );
  assert.equal(runtimeState.activeRequests, 0);
  assert.equal(runtimeState.bufferedRequests, 0);

  // Provider and admin capabilities are transport-bound.
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${upstreamToken}` },
  });
  assert.equal(response.status, 401);
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(response.status, 401);
  let unixResponse = await unixRequest(runtimeSocket, "/providers", {
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(unixResponse.status, 401);
  unixResponse = await unixRequest(adminSocket, "/providers", {
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(unixResponse.status, 200);
  assert.deepEqual(unixResponse.json(), gatewayCatalog);
  const hostileProviderSockets = await Promise.all(
    Array.from({ length: 64 }, () => connectUnix(runtimeSocket)),
  );
  unixResponse = await unixRequest(otherRuntimeSocket, "/health");
  assert.equal(unixResponse.status, 200);
  assert.equal(unixResponse.headers["x-cyclo-runtime-boot-id"], runtimeBootId);
  assert.equal(unixResponse.text(), "ok\n");
  controlResponse = await unixRequest(adminSocket, "/_cyclo/v1/control/reload", {
    method: "POST",
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(controlResponse.status, 204);
  for (const socket of hostileProviderSockets) socket.destroy();
  await new Promise((resolve) => setImmediate(resolve));
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${foreignTeamToken}` },
  });
  assert.equal(response.status, 401, "team bearer must match its private network interface");
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${unboundTeamToken}` },
  });
  assert.equal(response.status, 401, "missing team network binding must fail closed");

  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: "Bearer invalid-provider-capability",
      "content-type": "application/json",
    },
    body: "{}",
  });
  assert.equal(unixResponse.status, 401);
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: "Bearer another-invalid-capability",
      "content-type": "application/json",
    },
    body: "{}",
  });
  assert.equal(unixResponse.status, 429, "pre-auth registration floods must be throttled");
  assert.equal(
    unixResponse.text(),
    "provider registration request is already active or rate limited\n",
  );
  runtimeState.registrationRequestAt.fusion = 0;

  // A hostile component cannot register by pointing its writable endpoint at
  // the runtime's own healthy socket.
  symlinkSync(runtimeSocket, providerSocket);
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      version: 1,
      generation,
      api: "openai-responses",
      models: [{ id: "fused-model", name: "Fused model", input: ["text"] }],
    }),
  });
  assert.equal(unixResponse.status, 502);
  assert.match(unixResponse.text(), /not a real Unix socket/u);
  rmSync(providerSocket, { force: true });
  runtimeState.registrationRequestAt.fusion = 0;
  runtimeState.changedRegistrationAt.fusion = Number.NEGATIVE_INFINITY;
  runtimeState.changedRegistrationGlobalAt = Number.NEGATIVE_INFINITY;

  const providerRequests = [];
  let providerHealthRequests = 0;
  let staleFailureGate = null;
  provider = createServer(async (request, providerResponse) => {
    if (request.method === "GET" && request.url === "/health") {
      providerHealthRequests += 1;
      providerResponse.writeHead(200, { "content-type": "text/plain" });
      providerResponse.end("ok\n");
      return;
    }
    if (request.url?.includes("hang=one")) {
      for await (const _chunk of request) {
        // Consume the request while deliberately never producing a response.
      }
      return;
    }
    if (request.url?.includes("stale-fail=one")) {
      for await (const _chunk of request) {
        // Consume the request before exposing the controlled stale failure.
      }
      const gate = staleFailureGate;
      if (!gate) throw new Error("missing stale failure gate");
      gate.started();
      await gate.wait;
      request.socket.destroy();
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = Buffer.concat(chunks).toString("utf8");
    const context = request.headers["x-cyclo-request-context"];
    providerRequests.push({
      path: request.url,
      authorization: request.headers.authorization,
      context,
      body,
    });
    const transformed = JSON.parse(body);
    transformed.model = "input-model";
    const nested = await unixRequest(runtimeSocket, `/p/upstream${request.url}`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${upstreamToken}`,
        "content-type": "application/json",
        "x-cyclo-request-context": context,
      },
      body: JSON.stringify(transformed),
    });
    providerResponse.writeHead(nested.status, {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "text/plain",
      "set-cookie": `provider_session=${ingressToken}`,
      "x-context-reflection": context,
      "x-api-key": ingressToken,
    });
    providerResponse.end(`context=${context};${nested.text()}`);
  });
  await listenUnix(provider, providerSocket);

  // PATH is opaque in the runtime; this intentionally does not exist in its
  // filesystem. Registration alone probes this component exactly once.
  const fusionRegistration = JSON.stringify({
    version: 1,
    generation,
    api: "openai-responses",
    models: [{
      id: "fused-model",
      name: "Fused model",
      input: ["text", "unsafe"],
      baseUrl: "http://attacker.invalid",
      apiKey: "must-not-persist",
    }],
  });
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: fusionRegistration,
  });
  assert.equal(unixResponse.status, 204);
  assert.equal(providerHealthRequests, 1);
  assert.equal(existsSync(registered), true);
  const persisted = JSON.parse(readFileSync(registered, "utf8"));
  assert.equal(persisted.providers.fusion.ingress_token, ingressToken);
  assert.match(persisted.providers.fusion.registration_id, /^[a-f0-9]{32}$/u);
  assert.match(persisted.providers.fusion.socket_identity, /^[0-9]+:[0-9]+$/u);
  assert.equal(persisted.providers.fusion.models[0].baseUrl, undefined);
  assert.equal(persisted.providers.fusion.models[0].apiKey, undefined);
  assert.deepEqual(persisted.providers.fusion.models[0].input, ["text"]);

  const revisionAfterRegistration = runtimeState.snapshot().revision;
  const persistedAfterRegistration = readFileSync(registered, "utf8");
  runtimeState.registrationRequestAt.fusion = 0;
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: fusionRegistration,
  });
  assert.equal(unixResponse.status, 204);
  assert.equal(runtimeState.snapshot().revision, revisionAfterRegistration);
  assert.equal(readFileSync(registered, "utf8"), persistedAfterRegistration);
  assert.equal(
    providerHealthRequests,
    1,
    "idempotent registration must not probe, persist, or rebuild routes",
  );
  runtimeState.registrationRequestAt.fusion = 0;
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: fusionRegistration,
  });
  assert.equal(unixResponse.status, 429, "exact registration floods must be throttled");
  assert.equal(unixResponse.text(), "provider registration is rate limited\n");
  assert.equal(runtimeState.snapshot().revision, revisionAfterRegistration);
  assert.equal(readFileSync(registered, "utf8"), persistedAfterRegistration);
  assert.equal(providerHealthRequests, 1);

  // A malformed changed expected-provider registry removes both the component
  // route and its upstream capability in the live snapshot. Repair restores
  // the already pinned recovery record without touching host.conf.
  const validExpectedAuthority = readFileSync(expected, "utf8");
  replaceFile(expected, "{malformed expected authority\n");
  await assert.rejects(runtimeState.reconcileAuthorityFiles(), /cannot parse/u);
  unixResponse = await unixRequest(adminSocket, "/providers", {
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(unixResponse.status, 200);
  assert.deepEqual(Object.keys(unixResponse.json()), ["upstream"]);
  unixResponse = await unixRequest(runtimeSocket, "/providers", {
    headers: { authorization: `Bearer ${upstreamToken}` },
  });
  assert.equal(unixResponse.status, 401);
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 401);
  replaceFile(expected, validExpectedAuthority);
  await runtimeState.reconcileAuthorityFiles();
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()).sort(), ["fusion", "upstream"]);

  runtimeState.registrationRequestAt.fusion = 0;
  runtimeState.changedRegistrationAt.fusion = Date.now();
  runtimeState.changedRegistrationGlobalAt = Date.now();
  const changedRegistration = JSON.stringify({
    version: 1,
    generation,
    api: "openai-responses",
    models: [{ id: "fused-model", name: "Changed too quickly", input: ["text"] }],
  });
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: changedRegistration,
  });
  assert.equal(unixResponse.status, 429);
  assert.equal(
    unixResponse.text(),
    "provider registration is already active or rate limited\n",
  );
  assert.equal(providerHealthRequests, 1, "registration churn must be rejected before probing");

  // A provider bearer is only an input capability. Without a live request
  // context, even incomplete inference bodies are rejected before they can
  // occupy the project request/body pools and starve unrelated teams.
  const contextlessProviderRequests = Array.from({ length: 12 }, (_, index) => {
    const held = streamingUnixRequest(
      runtimeSocket,
      `/p/upstream/v1/responses?contextless=${index}`,
      {
        headers: {
          authorization: `Bearer ${upstreamToken}`,
          "content-type": "application/json",
          "content-length": "1024",
        },
      },
    );
    held.request.write('{"model":"input-model","input":"');
    return held;
  });
  const contextlessProviderResponses = await Promise.all(
    contextlessProviderRequests.map((held) => held.response),
  );
  assert.deepEqual(
    contextlessProviderResponses.map((item) => item.status),
    Array(12).fill(403),
  );
  assert.ok(contextlessProviderResponses.every(
    (item) => item.text() === "provider inference requires a live request context\n",
  ));
  assert.equal(runtimeState.activeRequests, 0);
  assert.equal(runtimeState.bufferedRequests, 0);
  for (const held of contextlessProviderRequests) held.request.destroy();
  response = await fetch(`${runtimeBase}/p/upstream/v1/responses?after-contextless=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "input-model", input: "still-live" }),
  });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "physical-response");

  // One hostile team can hold only its own bounded share of incomplete
  // request bodies. Another team and the host control path retain service.
  const heldRequests = [];
  for (let index = 0; index < 8; index += 1) {
    const held = streamingRequest(
      `${runtimeBase}/p/fusion/v1/responses?held=${index}`,
      {
        headers: {
          authorization: `Bearer ${limitedTeamToken}`,
          "content-type": "application/json",
          "content-length": "1024",
        },
      },
    );
    held.request.write('{"model":"fused-model","input":"');
    heldRequests.push(held);
  }
  await waitFor(
    () => runtimeState.activeRequests === 8 && runtimeState.bufferedRequests === 8,
  );
  response = await fetch(`${runtimeBase}/p/fusion/v1/responses?over-limit=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${limitedTeamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "fused-model", input: "over-limit" }),
  });
  assert.equal(response.status, 429);
  assert.equal(await response.text(), "client request concurrency limit exceeded\n");
  response = await fetch(`${runtimeBase}/p/upstream/v1/responses?other-team=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "input-model", input: "still-live" }),
  });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "physical-response");
  for (const held of heldRequests) held.request.destroy();
  await Promise.allSettled(heldRequests.map((held) => held.response));
  await waitFor(
    () => runtimeState.activeRequests === 0 && runtimeState.bufferedRequests === 0,
  );

  // Incomplete bodies have their own deadline, independent of the much longer
  // model-response deadline, and always release admission state.
  const timedBody = streamingRequest(
    `${runtimeBase}/p/fusion/v1/responses?slow-body=one`,
    {
      headers: {
        authorization: `Bearer ${limitedTeamToken}`,
        "content-type": "application/json",
        "content-length": "1024",
      },
    },
  );
  timedBody.request.write('{"model":"fused-model","input":"');
  await waitFor(
    () => runtimeState.activeRequests === 1 && runtimeState.bufferedRequests === 1,
  );
  const bodyDeadlineStarted = Date.now();
  const timedBodyOutcome = await Promise.race([
    timedBody.response
      .then((item) => ({ status: item.status }))
      .catch(() => ({ closed: true })),
    new Promise((_, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("inbound request body deadline did not fire")),
        5_000,
      );
      timeout.unref?.();
    }),
  ]);
  assert.ok(timedBodyOutcome.closed || timedBodyOutcome.status === 408);
  assert.ok(Date.now() - bodyDeadlineStarted < 5_000);
  timedBody.request.destroy();
  await waitFor(
    () => runtimeState.activeRequests === 0 && runtimeState.bufferedRequests === 0,
  );

  // Nested work has a separate budget charged to the originating project. A
  // saturated composed route cannot multiply one team across provider
  // identities and consume the root-request pool used by another team.
  let announceChainSaturation;
  const chainSaturated = new Promise((resolve) => {
    announceChainSaturation = resolve;
  });
  let releaseChain;
  const chainWait = new Promise((resolve) => {
    releaseChain = resolve;
  });
  chainGate = {
    arrivals: 0,
    expected: 8,
    started: announceChainSaturation,
    wait: chainWait,
  };
  const chainedRequests = Array.from({ length: 8 }, (_, index) => fetch(
    `${runtimeBase}/p/fusion/v1/responses?admission-chain=one&request=${index}`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${limitedTeamToken}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ model: "fused-model", input: `chain-${index}` }),
    },
  ));
  await chainSaturated;
  assert.equal(runtimeState.activeRequests, 8);
  assert.equal(runtimeState.nestedRequests, 8);
  assert.equal(runtimeState.bufferedRequests, 8);
  assert.equal(runtimeState.nestedBufferedRequests, 8);
  response = await fetch(`${runtimeBase}/p/upstream/v1/responses?other-origin=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "input-model", input: "unstarved" }),
  });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "physical-response");
  releaseChain();
  const chainedResponses = await Promise.all(chainedRequests);
  assert.deepEqual(chainedResponses.map((item) => item.status), Array(8).fill(200));
  chainGate = null;
  await waitFor(() => (
    runtimeState.activeRequests === 0
    && runtimeState.nestedRequests === 0
    && runtimeState.bufferedRequests === 0
    && runtimeState.nestedBufferedRequests === 0
  ));

  // An exact 204 is a volatile lease barrier. A dispatch failure that started
  // before it must not delete the newly reaffirmed registration afterward.
  let announceStaleFailure;
  const staleFailureStarted = new Promise((resolve) => {
    announceStaleFailure = resolve;
  });
  let releaseStaleFailure;
  const staleFailureRelease = new Promise((resolve) => {
    releaseStaleFailure = resolve;
  });
  staleFailureGate = {
    started: announceStaleFailure,
    wait: staleFailureRelease,
  };
  runtimeState.registrationRequestAt.fusion = 0;
  runtimeState.exactRegistrationAt.fusion = 0;
  const staleDispatch = fetch(`${runtimeBase}/p/fusion/v1/responses?stale-fail=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${limitedTeamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "fused-model", input: "stale-dispatch" }),
  });
  await staleFailureStarted;
  unixResponse = await unixRequest(runtimeSocket, "/_cyclo/v1/providers/fusion", {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: fusionRegistration,
  });
  assert.equal(unixResponse.status, 204);
  releaseStaleFailure();
  response = await staleDispatch;
  assert.equal(response.status, 502);
  assert.equal(await response.text(), "provider runtime upstream request failed\n");
  staleFailureGate = null;

  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${limitedTeamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()), ["fusion"]);

  // A virtual-only team cannot bypass the declared front-door scope and call
  // the concrete dependency directly.
  response = await fetch(`${runtimeBase}/p/upstream/v1/responses`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${limitedTeamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "input-model", input: "bypass" }),
  });
  assert.equal(response.status, 403);

  response = await fetch(`${runtimeBase}/p/fusion/v1/responses?nested=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${limitedTeamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "fused-model", input: "compose" }),
  });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "context=[REDACTED];physical-response");
  assert.equal(response.headers.has("authorization"), false);
  assert.equal(response.headers.has("set-cookie"), false);
  assert.equal(response.headers.has("x-context-reflection"), false);
  assert.equal(response.headers.has("x-api-key"), false);
  assert.equal(providerRequests.at(-1).authorization, `Bearer ${ingressToken}`);
  assert.notEqual(providerRequests.at(-1).authorization, `Bearer ${limitedTeamToken}`);
  assert.match(providerRequests.at(-1).context, /^[A-Za-z0-9_-]{40,}$/u);
  const physical = gatewayRequests.filter((item) => item.method === "POST").at(-1);
  assert.equal(physical.authorization, `Bearer ${limitedTeamToken}`);
  assert.equal(physical.context, undefined);

  // The normal request path is completely detached from mounted configuration
  // and recovery files. Even malformed replacements are invisible until the
  // corresponding explicit control operation or process restart.
  const cachedFiles = new Map(
    [hostConfig, clients, expected, registered].map((path) => [path, readFileSync(path, "utf8")]),
  );
  try {
    for (const path of cachedFiles.keys()) writeFileSync(path, "{not-runtime-state\n");
    response = await fetch(`${runtimeBase}/providers`, {
      headers: { authorization: `Bearer ${limitedTeamToken}` },
    });
    assert.equal(response.status, 200);
    assert.deepEqual(Object.keys(await response.json()), ["fusion"]);
    response = await fetch(`${runtimeBase}/p/fusion/v1/responses?cached=one`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${limitedTeamToken}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ model: "fused-model", input: "cached-state" }),
    });
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "context=[REDACTED];physical-response");
  } finally {
    for (const [path, content] of cachedFiles) writeFileSync(path, content);
  }

  // The dispatch deadline covers the component request and its response body;
  // a component that never answers cannot hold a runtime request forever.
  const timeoutStarted = Date.now();
  response = await fetch(`${timeoutRuntimeBase}/p/fusion/v1/responses?hang=one`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${limitedTeamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "fused-model", input: "timeout" }),
  });
  assert.equal(response.status, 502);
  assert.equal(await response.text(), "provider runtime upstream request failed\n");
  assert.ok(Date.now() - timeoutStarted < 1_000, "component dispatch must honor its deadline");

  // Provider input catalog is exact and available only on the provider UDS.
  unixResponse = await unixRequest(runtimeSocket, "/providers", {
    headers: { authorization: `Bearer ${upstreamToken}` },
  });
  assert.equal(unixResponse.status, 200);
  assert.deepEqual(Object.keys(unixResponse.json()), ["upstream"]);
  assert.equal(
    gatewayCatalogFetches,
    1,
    "normal catalog and proxy requests must use the startup concrete catalog",
  );
  assert.equal(
    providerHealthRequests,
    1,
    "normal catalog and proxy requests must not health-probe a registered component",
  );

  // A new process can recover a durable registration, but only after probing
  // the pinned component endpoint once during startup.
  const recoveredState = await createRuntimeState();
  assert.deepEqual(
    Object.keys(recoveredState.snapshot().catalog).sort(),
    ["fusion", "upstream"],
  );
  assert.equal(gatewayCatalogFetches, 2);
  assert.equal(providerHealthRequests, 2);

  // host.conf belongs to the process-lifetime snapshot. Editing the mounted
  // file does not mutate a running router, while a newly created state (the
  // restart boundary) observes the edit.
  replaceFile(
    hostConfig,
    "provider fusion /host/source/not-mounted upstream/input-model strength=2\n",
  );
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()).sort(), ["fusion", "upstream"]);
  assert.equal(runtimeState.snapshot().configuration.byPrefix.fusion.prefix, "fusion");
  const restartedState = await createRuntimeState();
  assert.deepEqual(Object.keys(restartedState.snapshot().catalog), ["upstream"]);
  assert.equal(restartedState.snapshot().configuration.byPrefix.fusion.parameters[0][1], "2");
  assert.equal(gatewayCatalogFetches, 3);
  assert.equal(providerHealthRequests, 2, "changed restart configuration cannot probe fusion");

  // Endpoint liveness is cached until dispatch. A dead or replaced component
  // remains in the catalog, then the failed dispatch drops exactly that route.
  await close(provider);
  provider = null;
  rmSync(providerSocket, { force: true });
  symlinkSync(runtimeSocket, providerSocket);
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()).sort(), ["fusion", "upstream"]);
  response = await fetch(`${runtimeBase}/p/fusion/v1/responses`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${teamToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "fused-model", input: "must-not-dispatch" }),
  });
  assert.equal(response.status, 502);
  assert.equal(await response.text(), "provider runtime upstream request failed\n");
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()), ["upstream"]);
  assert.equal(
    JSON.parse(readFileSync(registered, "utf8")).providers.fusion,
    undefined,
    "dropping the dead cached route is persisted",
  );

  // Dynamic authority replacement deliberately retains both startup
  // host.conf and the concrete catalogue. The normal control call gives the
  // controller a synchronous acknowledgment; the background watcher uses the
  // same transaction to close a controller-crash window after the durable
  // files have changed.
  const replacementTeamToken = "team-replacement-capability";
  writeFileSync(clients, `${JSON.stringify({
    version: 1,
    clients: [{
      kind: "client",
      client_id: "team-replacement",
      team_id: "team-replacement",
      binding_generation: "team-generation-two",
      token_sha256: hash(replacementTeamToken),
      providers: ["upstream", "newleaf"],
      models: ["upstream/input-model", "newleaf/new-model"],
      local_addresses: ["127.0.0.1"],
      enabled: true,
      revoked: false,
      expires_at: null,
    }],
  }, null, 2)}\n`);
  writeFileSync(expected, `${JSON.stringify({ version: 1, providers: [] }, null, 2)}\n`);
  const validReplacementClients = readFileSync(clients, "utf8");
  gatewayCatalog.newleaf = {
    api: "openai-responses",
    models: [{ id: "new-model", name: "New concrete model", input: ["text"] }],
  };
  assert.equal(runtimeState.snapshot().expected.fusion.generation, generation);

  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 200);
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 401);

  runtimeState.startAuthorityWatch(20);
  await waitFor(
    () => runtimeState.snapshot().clients[0]?.client_id === "team-replacement",
  );
  runtimeState.stopAuthorityWatch();
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 401, "authority watcher must revoke the old bearer");
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()), ["upstream"]);

  controlResponse = await unixRequest(adminSocket, "/_cyclo/v1/control/reload", {
    method: "POST",
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(controlResponse.status, 204);
  assert.equal(gatewayCatalogFetches, 3, "capability reload must not call the gateway");
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${teamToken}` },
  });
  assert.equal(response.status, 401);
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()), ["upstream"]);
  assert.equal(runtimeState.snapshot().expected.fusion, undefined);

  replaceFile(clients, "{malformed authority\n");
  await assert.rejects(
    runtimeState.reconcileAuthorityFiles(),
    /cannot parse/u,
  );
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 401, "malformed changed authority must fail closed");
  replaceFile(clients, validReplacementClients);
  await runtimeState.reconcileAuthorityFiles();
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 200);

  // Concrete catalogue refresh is a separate transaction. A gateway outage
  // can never block a capability revocation. While this fetch is blocked,
  // requests continue on the preceding immutable snapshot.
  let announceCatalogFetch;
  const catalogFetchStarted = new Promise((resolve) => {
    announceCatalogFetch = resolve;
  });
  let releaseCatalogFetch;
  const catalogFetchGate = new Promise((resolve) => {
    releaseCatalogFetch = resolve;
  });
  nextCatalogGate = { started: announceCatalogFetch, wait: catalogFetchGate };
  const refresh = unixRequest(adminSocket, "/_cyclo/v1/control/refresh-catalog", {
    method: "POST",
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  await catalogFetchStarted;

  const reloadWhileRefreshIsBlocked = await Promise.race([
    unixRequest(adminSocket, "/_cyclo/v1/control/reload", {
      method: "POST",
      headers: { authorization: `Bearer ${runtimeAdminToken}` },
    }),
    new Promise((_, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("catalog refresh blocked capability reload")),
        250,
      );
      timeout.unref?.();
    }),
  ]);
  assert.equal(reloadWhileRefreshIsBlocked.status, 204);

  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()), ["upstream"]);

  releaseCatalogFetch();
  response = await refresh;
  assert.equal(response.status, 204);
  assert.equal(gatewayCatalogFetches, 4);
  response = await fetch(`${runtimeBase}/providers`, {
    headers: { authorization: `Bearer ${replacementTeamToken}` },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(await response.json()).sort(), ["newleaf", "upstream"]);
  assert.equal(
    runtimeState.snapshot().configuration.byPrefix.fusion.prefix,
    "fusion",
    "control reload must not reload host.conf",
  );
  assert.equal(providerHealthRequests, 2, "control reload must not probe components");

  // Refresh completion order cannot roll the catalogue backward. The newer
  // request commits first; the older response is ignored when it arrives.
  let announceOlderCatalog;
  const olderCatalogStarted = new Promise((resolve) => {
    announceOlderCatalog = resolve;
  });
  let releaseOlderCatalog;
  const olderCatalogWait = new Promise((resolve) => {
    releaseOlderCatalog = resolve;
  });
  let announceNewerCatalog;
  const newerCatalogStarted = new Promise((resolve) => {
    announceNewerCatalog = resolve;
  });
  let releaseNewerCatalog;
  const newerCatalogWait = new Promise((resolve) => {
    releaseNewerCatalog = resolve;
  });
  catalogGateQueue.push(
    {
      started: announceOlderCatalog,
      wait: olderCatalogWait,
      document: {
        ...gatewayCatalog,
        staleonly: {
          api: "openai-responses",
          models: [{ id: "stale", name: "Stale", input: ["text"] }],
        },
      },
    },
    {
      started: announceNewerCatalog,
      wait: newerCatalogWait,
      document: {
        ...gatewayCatalog,
        newest: {
          api: "openai-responses",
          models: [{ id: "fresh", name: "Fresh", input: ["text"] }],
        },
      },
    },
  );
  const olderRefresh = runtimeState.refreshCatalog();
  await olderCatalogStarted;
  const newerRefresh = runtimeState.refreshCatalog();
  await newerCatalogStarted;
  releaseNewerCatalog();
  await newerRefresh;
  releaseOlderCatalog();
  await olderRefresh;
  assert.equal(Object.hasOwn(runtimeState.snapshot().concrete, "newest"), true);
  assert.equal(Object.hasOwn(runtimeState.snapshot().concrete, "staleonly"), false);

  // A concrete account that collides with a configured component prefix is
  // omitted entirely. Ambiguity must never silently select the concrete route.
  gatewayCatalog.fusion = {
    api: "openai-responses",
    models: [{ id: "fused-model", name: "Colliding physical model", input: ["text"] }],
  };
  controlResponse = await unixRequest(adminSocket, "/_cyclo/v1/control/refresh-catalog", {
    method: "POST",
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(controlResponse.status, 204);
  unixResponse = await unixRequest(adminSocket, "/providers", {
    headers: { authorization: `Bearer ${runtimeAdminToken}` },
  });
  assert.equal(unixResponse.status, 200);
  assert.equal(Object.hasOwn(unixResponse.json(), "fusion"), false);
  unixResponse = await unixRequest(adminSocket, "/p/fusion/v1/responses", {
    method: "POST",
    headers: {
      authorization: `Bearer ${runtimeAdminToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ model: "fused-model", input: "collision" }),
  });
  assert.equal(unixResponse.status, 404);
  assert.equal(unixResponse.text(), "unknown provider: fusion\n");

  // A refresh whose gateway fetch completed but whose queued commit was then
  // aborted must not mutate the active catalogue later.
  let announceMutationBlock;
  const mutationBlockStarted = new Promise((resolve) => {
    announceMutationBlock = resolve;
  });
  let releaseMutationBlock;
  const mutationBlockRelease = new Promise((resolve) => {
    releaseMutationBlock = resolve;
  });
  const mutationBlock = runtimeState.serialize(async () => {
    announceMutationBlock();
    await mutationBlockRelease;
  });
  await mutationBlockStarted;
  const blockedTail = runtimeState.writeTail;
  gatewayCatalog.late = {
    api: "openai-responses",
    models: [{ id: "late-model", name: "Late model", input: ["text"] }],
  };
  const refreshAbort = new AbortController();
  const lateRefresh = runtimeState.refreshCatalog(refreshAbort.signal);
  const lateRefreshRejected = assert.rejects(lateRefresh, /aborted before commit/u);
  await waitFor(() => runtimeState.writeTail !== blockedTail);
  refreshAbort.abort(new Error("aborted before commit"));
  releaseMutationBlock();
  await mutationBlock;
  await lateRefreshRejected;
  assert.equal(Object.hasOwn(runtimeState.snapshot().concrete, "late"), false);
  delete gatewayCatalog.late;
  delete gatewayCatalog.fusion;

  // Registration is never exposed on the team-facing TCP listener.
  response = await fetch(`${runtimeBase}/_cyclo/v1/providers/fusion`, {
    method: "PUT",
    headers: {
      authorization: `Bearer ${ingressToken}`,
      "content-type": "application/json",
    },
    body: "{}",
  });
  assert.equal(response.status, 403);
});


test("provider runtime source has no credential-gateway or pi-ai dependency", () => {
  const source = readFileSync(
    new URL("../src/cyclo/provider_runtime_context/server.mjs", import.meta.url),
    "utf8",
  );
  const packageDocument = JSON.parse(readFileSync(
    new URL("../src/cyclo/provider_runtime_context/package.json", import.meta.url),
    "utf8",
  ));
  assert.equal(packageDocument.dependencies && Object.keys(packageDocument.dependencies).length, 0);
  for (const forbidden of ["pi-ai", "credential_gateway", "AUTH_JSON", "credentialStore"] ) {
    assert.equal(source.includes(forbidden), false, `runtime source must not contain ${forbidden}`);
  }
});
