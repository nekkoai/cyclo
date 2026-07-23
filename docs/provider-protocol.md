# Cyclo Provider protocol v1

Cyclo composes model providers as ordinary components connected by named
interfaces. The Provider interface deliberately separates its control plane
from its inference data plane.

## Component graph

Every provider repository contains a Docker build context and a short
`component.conf`:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

`provide` declares services implemented by the component. `require` declares a
named input. The installation's `host.conf` binds each requirement to `gateway`
or an earlier component:

```text
provider first ./providers/pass upstream=gateway
provider second ./providers/pass upstream=first
```

The gateway is always the root Provider. An empty `host.conf` selects its
socket directly. Otherwise Cyclo selects the last working component whose
declared inputs are also working. A build or startup failure leaves an earlier
working provider selected—possibly the gateway—while status exposes the failed
component.

Cyclo builds and starts components in declaration order. Each component gets a
writable output socket directory at `/run/cyclo` and one read-only producer
socket directory per requirement at
`/run/cyclo/requirements/NAME`. Components run with `--network none`; the
socket mount is the edge capability. They receive no sibling sockets, Docker
socket, team files, project files, or gateway credential volume.

## Transport

Services use ConnectRPC over HTTP/1.1 Unix-domain sockets. The component
interface provides the common `Health` RPC. The Provider interface provides:

```proto
service Provider {
  rpc ListModels(ListModelsRequest) returns (ListModelsResponse);
  rpc Infer(InferRequest) returns (stream InferResponse);
}
```

ConnectRPC supplies framing, streaming, backpressure, deadlines, cancellation,
and transport errors. Cyclo does not add bearer tokens between mounted
component sockets.

## Typed control plane

`ListModels` returns the model IDs accepted by that component, display data,
capabilities, token limits, and an `inference_format` identifier. Cyclo reads
this information to assemble the catalogue and Pi reads it to register models.

Model IDs are opaque strings. A component that aliases, combines, or selects
upstreams publishes its own IDs. A pure relay returns the upstream catalogue
unchanged. Components must not connect an input whose `inference_format` they
do not implement.

Version 1 uses:

```text
pi-ai@0.81.1
```

The identifier makes the data-plane ABI explicit. Updating the pinned Pi
version requires updating every endpoint that encodes or decodes its payload;
relays remain unaffected.

## Opaque inference data plane

The complete wire messages are:

```proto
message InferRequest {
  string model = 1;
  string payload = 2;
}

message InferResponse {
  string payload = 1;
}
```

Cyclo understands `model` because it must select a route. It does not understand
`payload`.

The team-side Pi extension serializes one call frame:

```json
{
  "context": { "systemPrompt": "...", "messages": [], "tools": [] },
  "options": { "reasoning": "high", "maxTokens": 4096 }
}
```

This is Pi data, not a Cyclo inference schema. Message roles, content blocks,
tool definitions, JSON Schema, signatures, reasoning state, tool arguments,
provider-specific options, and future fields pass unchanged. A relay must not
parse, normalize, validate, redact, reorder, or reserialize the string.

At the root gateway, the Pi endpoint parses the call frame once because it must
invoke the in-process `pi-ai` API. It passes `context` and the JSON inference
options to the pinned native `streamSimple` implementation. It supplies the
native model, credential, abort signal, retry policy, and transport controls
from gateway-owned state.

The following local/security controls never come from inference data:

- `signal` and the caller deadline travel as ConnectRPC call controls;
- `apiKey` comes from the gateway credential store;
- arbitrary HTTP headers and provider environment values are gateway-owned;
- injected client objects and JavaScript callbacks cannot cross the wire; and
- native transport, HTTP/WebSocket timeout, and retry controls are
  gateway-owned.

All other JSON options are forwarded without a Cyclo allowlist.

For each native Pi `AssistantMessageEvent`, the gateway serializes one response
payload. The team-side endpoint parses that string and pushes the event directly
into Pi. Cyclo does not impose another event state machine or finish-reason
enumeration.

## Errors

The error boundary is simple:

- unknown outer model: Connect `NOT_FOUND`;
- invalid JSON or missing Pi call framing at the terminating endpoint: Connect
  `INVALID_ARGUMENT`;
- unavailable component, credential, or concrete service: Connect
  `UNAVAILABLE`;
- cancellation or deadline: the corresponding Connect error;
- a provider/model failure already represented as a Pi event: an opaque response
  payload.

Relays propagate Connect errors unchanged and never retry a partially emitted
stream. The team-side endpoint converts a transport failure into a terminal Pi
error event because Pi's local stream API requires a terminal result.

## Semantic provider components

Opaque transport does not prevent useful components. It makes interpretation
explicit.

- A pass-through or multiplexer can route using the outer model and forward the
  payload strings untouched.
- A recorder can store opaque strings without understanding them.
- A fusion, policy, or transformation component may deliberately terminate the
  Pi payload ABI, inspect it, perform upstream calls, and emit new Pi events.

Such a component owns and documents those semantics. They are not silently
imposed on every Cyclo request by the transport layer.

## Required tests

A Provider implementation should test:

- exact request and response payload-string preservation for relays;
- arbitrary and future Pi fields, including unrestricted tool schemas;
- streamed delivery without buffering;
- cancellation propagation and cleanup;
- isolation from incoming HTTP authorization/cookie headers;
- catalogue inference-format compatibility; and
- generic health behavior when an upstream disappears and returns.
