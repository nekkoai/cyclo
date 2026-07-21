# Cyclo provider protocol v1

This is the container contract for an intermediate provider named by a
`provider` line in `host.conf`. A component transforms one or more already
available input models into models under its own output prefix.

The peer in this protocol is the **provider runtime**, not the credential
gateway. The gateway remains a separate Cyclo security service that owns real
credentials and concrete upstream traffic. Components neither register with
the gateway nor connect to it.

Building an installed provider executes its `Dockerfile` and is therefore a
host-administration action. Keep provider source outside agent-writable team
and workspace mounts, review it, and pin it through your deployment process.
Runtime isolation cannot make an unsafe Dockerfile safe to build.

## Transport and isolation

Protocol v1 is HTTP/1.1 over Unix-domain stream sockets. Provider containers run
with Docker `--network none`; they have no routable path to the host, Internet,
teams, gateway, or another provider. Cyclo publishes no provider port and
mounts no Docker socket, team tree, project tree, or gateway credential volume.

There are two socket directions:

- the provider runtime creates a different `runtime.sock` in a different
  host-owned subdirectory for every prefix; each component receives only its
  own subdirectory as a read-only mount at `/run/cyclo/runtime`;
- each component gets a different writable directory for `provider.sock`, and
  only the provider runtime receives a read-only mount of that directory.

The host controller uses a third, mode-`0600` control socket in the private socket
root; no provider mounts that root or control socket. Components never mount one
another's directories. Provider socket inodes use mode `0666` so unrelated
unprivileged image UIDs interoperate; distinct directory mounts control
reachability and bearer capabilities control authority. HTTP preserves
streaming, backpressure, cancellation, headers, and language-neutral framing.

The runtime rejects symlink endpoints and pins the component socket device/inode
when registration is accepted or recovered. It rechecks the selected socket's
pinned identity for every inference dispatch. The path check and Unix `connect`
are not one kernel-atomic operation; the private runtime-socket pathname and
per-component ingress capabilities make a remaining swap race a
denial-of-service path rather than a route to the runtime control socket or
another component's authority.

## Process invocation

The image must define a nonempty OCI `ENTRYPOINT`. Cyclo passes every token
after `PATH` in the corresponding `host.conf` line as a separate argument and
sets:

```text
CYCLO_PROVIDER_PROTOCOL=1
CYCLO_PROVIDER_PREFIX=<host-selected output prefix>
CYCLO_PROVIDER_GENERATION=<implementation-and-arguments digest>
CYCLO_PROVIDER_RUNTIME_SOCKET=/run/cyclo/runtime/runtime.sock
CYCLO_PROVIDER_SOCKET=/run/cyclo/self/provider.sock
CYCLO_PROVIDER_TOKEN_FILE=/run/secrets/cyclo-provider-token
CYCLO_UPSTREAM_TOKEN_FILE=/run/secrets/cyclo-upstream-token
```

Both tokens are mounted as read-only regular files. They never appear in argv
or environment values:

- the **provider token** is a route-local ingress capability. It authenticates
  registration and provider-runtime-to-component inference;
- the distinct **upstream token** authenticates component-to-runtime catalogue
  and inference calls. It is bound to the component prefix and generation and
  scoped to the exact input models declared in `host.conf`.

Neither token is a physical provider credential, team bearer, or gateway
capability. Components have no transport to gateway catalogue, usage, or
inference endpoints. An explicit
`cyclo provider restart PREFIX` first revokes the old route and upstream
authority, then stops the old process, rotates both token files, and only then
publishes replacement authority.

## Startup and registration

The component must fail startup if its arguments, environment, token files, or
declared inputs are invalid. Its bootstrap sequence is:

1. Remove only its own stale `CYCLO_PROVIDER_SOCKET`, listen there, and set the
   socket mode to `0666`.
2. Optionally read its input-only catalogue from `GET /providers` on
   `CYCLO_PROVIDER_RUNTIME_SOCKET`, using the upstream token.
3. Register with the provider runtime. Registration succeeds only with an
   empty HTTP 204 response. HTTP 429 means an exact renewal or changed
   registration is inside its bounded rate interval and must be retried.

A component should retry catalogue discovery and registration with short,
bounded backoff for at least 30 seconds. This tolerates runtime/socket startup
races while keeping permanent configuration errors fatal.

The provider runtime probes the component over its private component socket
when accepting a registration and when recovering one after runtime startup:

```http
GET /health HTTP/1.1
```

The response must be status 200 with exactly `ok\n`. This probe needs no bearer
because only the provider runtime can mount that socket directory. Normal
catalogue and inference requests do not repeat health probes for every
configured component.

Registration is sent over `CYCLO_PROVIDER_RUNTIME_SOCKET`:

```http
PUT /_cyclo/v1/providers/PREFIX HTTP/1.1
Authorization: Bearer <provider token>
Content-Type: application/json

{
  "version": 1,
  "generation": "<CYCLO_PROVIDER_GENERATION>",
  "api": "openai-responses",
  "models": [
    { "id": "output-model", "name": "Output model", "input": ["text"] }
  ]
}
```

The document has exactly `version`, `generation`, `api`, and `models` fields,
is limited to 64 KiB, and may advertise at most 256 models. `models` must be
nonempty and use Cyclo's safe model-metadata projection. The
component cannot choose a socket path, input scope, base URL, header, or
credential. Cyclo has already authorized the prefix, generation, declared
inputs, token hash, and fixed socket path in provider-runtime state.

Registration is accepted only on the provider runtime's Unix transport, never
on its team-facing TCP listener. The runtime verifies the immutable startup
configuration and expected launch state, probes health, sanitizes metadata,
durably stores the sanitized registration solely for restart recovery, and
atomically replaces its active in-memory snapshot. Durable changed
registrations are limited per prefix and globally to bound synchronous disk
writes. Authenticated attempts are serialized and limited to ten per second per
prefix before their body is read. A component may re-register the exact current
document at most once per second; that idempotent 204 advances only a volatile
dispatch lease, without another health probe, disk write, or route-snapshot
rebuild. The lease prevents an older failed dispatch from deleting a
registration reaffirmed afterward.

At runtime startup, a persisted registration is only a recovery candidate. The
runtime must revalidate it against the newly parsed configuration and expected
launch state, verify the pinned socket identity, and repeat the health probe
before adding it to the active snapshot. Failure of any check leaves the route
inactive. A runtime-container replacement therefore requires neither a
component image rebuild nor persistence of the active route table itself.

## Catalogue and routing

Authenticated `GET /providers` returns a catalogue filtered to the caller's
capability. The provider runtime parses `host.conf` once at startup and builds
one immutable in-memory snapshot from it, the concrete gateway catalogue,
expected provider state, and validated recovery registrations. A component can
see only its declared inputs, and those inputs may be concrete gateway models or
outputs from earlier lines. The host refreshes the concrete gateway catalogue
through its private control Unix socket after gateway changes; it does not reload
`host.conf` or involve provider components.

A missing or empty `host.conf` exposes the concrete gateway catalogue unchanged
to authorized team clients. Every configuration edit requires an explicit
runtime restart before it takes effect; use `cyclo runtime restart`. No
configuration-only change requires an image rebuild, and configuration changes
never implicitly build, start, restart, or stop a component.

Each normal catalogue or inference request captures the current snapshot. It
does not reread configuration or registry files, refetch and reconstruct the
whole catalogue, or health-probe every component. Successful registration,
capability reload, or catalogue refresh publishes a new snapshot. Capability
reload and catalogue refresh are separate control operations, so revocation does
not depend on gateway availability. Inference dispatch still verifies the
selected component socket against its pinned device/inode.

Expected-provider and client registries are dynamic authority. Outside the
request path, the runtime compares their file identities every 500 ms and runs
the same capability reload after an atomic replacement. This bounds the
revocation gap if the host controller is killed after writing authority but
before sending the control request. A changed malformed registry fails closed
by revoking all dynamic clients and component routes until repaired. The
watcher never reads `host.conf`; provider configuration remains restart-only.

The host authenticates a distinct control capability on the mode-`0600` control
Unix socket. It can read the merged catalogue with `GET /providers` and use two
empty-body operations that require a `204` acknowledgement:

```text
POST /_cyclo/v1/control/reload
POST /_cyclo/v1/control/refresh-catalog
```

`reload` reads only the mounted expected-provider and client registries.
`refresh-catalog` reads only the concrete catalogue from the credential
gateway. Neither operation reparses `host.conf` or health-probes every
component. The control capability is rejected on every workload/inference
path, and provider and team capabilities cannot call control endpoints.
If a registry reload cannot be acknowledged, the host stops the runtime so an
old capability cannot remain live. A failed catalogue refresh instead leaves
the preceding safe snapshot running. Gateway login or restart may therefore
have completed even when its follow-up catalogue refresh reports an error.

If the security epoch changes while an inference body is being processed, the
request receives `503 provider runtime policy changed; retry request` and is
never dispatched under stale authority. Route-only registration and catalogue
updates do not invalidate unrelated live request contexts.

## Inference

A team sends a normal model request to the provider runtime:

```text
POST /p/OUTPUT_PREFIX/<native inference path>
Authorization: Bearer <team capability>
```

The host binds that capability to the provider runtime's local IP address on
the team's private Docker network. The runtime authenticates both the bearer
hash and the TCP destination interface. A bearer replayed from another team
network is rejected, and an absent binding fails closed.

A dead, replaced, or identity-mismatched component remains visible until it is
selected. That dispatch fails with a generic `502`; the runtime then removes
that exact registration from both the active table and restart-recovery state.
The component must register again before the route returns.

The v1 native-path allowlist is closed:

- `/chat/completions`, `/v1/chat/completions`;
- `/responses`, `/v1/responses`, `/codex/responses`,
  `/v1/codex/responses`;
- `/messages`, `/v1/messages`;
- `/v1internal:generateContent`, `/v1internal/generateContent`, and the
  corresponding `streamGenerateContent` forms; and
- Google model-action paths rooted exactly at `/models`, `/v1/models`,
  `/v1beta/models`, or the canonical Vertex
  `/v1/projects/.../locations/.../publishers/google/models` hierarchy.

One encoded slash is permitted inside a Google model ID. Decoded dot segments,
backslashes, whitespace, controls, and unrelated path prefixes are rejected.
Query parameters are preserved. Body-model APIs require exactly one top-level
JSON string `model`; model-in-path APIs use the advertised model in the path.

After validating the team's exact model scope, the provider runtime calls the
component's native path over its private socket with:

```text
Authorization: Bearer <provider token>
X-Cyclo-Request-Context: <opaque live context>
```

The component must authenticate the provider token with a timing-safe
comparison, require the request context, accept only its advertised models and
supported POST paths, and bound both encoded and decoded bodies. To invoke one
declared input, it sends the corresponding request back over
`CYCLO_PROVIDER_RUNTIME_SOCKET`:

```text
POST /p/INPUT_PREFIX/<native inference path>
Authorization: Bearer <upstream token>
X-Cyclo-Request-Context: <the same opaque context>
```

The context is valid only during the live outer request and is bound to the
component prefix, client identity, generation, and runtime policy epoch. The
epoch changes on capability-policy reloads; route/catalog-only snapshot
revisions do not invalidate a live context. The runtime rejects an upstream
token without a matching live context. Nested providers receive a child
context. One outer request admits at most 16 nested component calls and a chain
at most 16 components deep.

For a concrete input, the runtime recovers the original team bearer from its
in-memory context and forwards that bearer to the credential gateway. A
component sees only its ingress token, upstream token, and live context; it
never sees the original team bearer or a real credential. There is no direct
component-to-component route.

Components must not return `X-Cyclo-Request-Context` or any capability in a
response. The provider runtime drops sensitive response headers and redacts
exact token/context bytes reflected across streamed body chunks. Components
should apply the same defense, relay only safe headers, preserve response
streaming and backpressure, propagate cancellation, and keep tokens and request
bodies out of errors and logs.

Protocol requests are limited to 16 MiB in encoded and decoded form and must
finish their inbound body within 30 seconds. The shared runtime admits eight
root requests per project/provider principal and 24 globally, with at most 12
root bodies retained. Valid nested requests use a separate pool charged to the
request-context origin: 16 per project, 32 globally, and at most 24 nested
bodies retained. The TCP listener permits 32 connections per team-facing
interface and 256 globally. Every prefix has a separate UDS listener capped at
64 connections and a prefix-local 200 request/s token bucket; team interfaces
have a 500 request/s token bucket. Host control has its own inaccessible
UDS listener. A component should also bound concurrent transformations; Cyclo
runs it with a 512 MiB memory/swap limit, two CPUs, a PID limit, a read-only
root, and a small writable tmpfs.

## Accounting

The credential gateway records only concrete upstream requests. Because the
provider runtime forwards the original team bearer, every physical call keeps
the originating team/project generation, concrete account, and model
attribution. A virtual hop is not counted as a second token-consuming gateway
request. If a fusion or multiplexer deliberately makes several concrete calls,
each real call is recorded.
