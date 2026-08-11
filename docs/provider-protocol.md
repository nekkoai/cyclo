# Provider components and protocol

## Scope

Cyclo separates component wiring from application semantics:

- DComp declares component inputs and outputs, creates direct link networks,
  injects endpoint addresses, and owns Docker lifecycle.
- Cyclo defines the `cyclo.provider.v1.Provider` application interface and
  compiles gateway, provider, terminal edge, and team components into one
  DComp system.
- Provider implementations decide what transformation or policy to apply.

DComp does not inspect Provider messages. Cyclo does not proxy inference
traffic on the host.

## Component source

A provider source is a directory containing `component.dcomp`:

```text
docker example/passthrough:1
input cyclo.provider.v1.Provider upstream
output cyclo.provider.v1.Provider provider
```

The Provider-declaration grammar is:

```text
docker IMAGE
input PROTOBUF_SERVICE LOCAL_NAME
output PROTOBUF_SERVICE LOCAL_NAME
```

Blank lines and text following `#` are ignored. `docker` appears exactly once.
Input and output names are local to the component and use lower-case letters,
digits, and hyphens. Service names are fully qualified protobuf service names.

Cyclo requires every installed provider to expose exactly one
`cyclo.provider.v1.Provider` output. It may declare additional application
interfaces. Every input must be bound by `host.conf`.

If the directory contains a `Dockerfile`, Cyclo builds it. Otherwise `IMAGE`
must already be present in the selected local Docker Engine. The image must
define an OCI health check and listen for its declared interfaces on TCP port
50051.

Cyclo also recognizes the source token `pooler`. It resolves to Cyclo's
installed pooler source and uses the installed components directory as its
fixed Docker build context. A `context=PATH` override on that bundled source is
rejected.

## Host realm configuration

Install component instances in `host.conf`:

```text
provider trace ./providers/passthrough upstream=gateway.provider -- label=trace
provider policy ./providers/policy upstream=trace.provider
```

The grammar is:

```text
provider NAME SOURCE [context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARGUMENT ...]
```

- `gateway.provider` is the fixed root output.
- Relative source paths resolve beside `host.conf`; `~` is not expanded.
- `pooler` selects the packaged pooler source.
- `context=PATH` selects a Docker build context containing `SOURCE`.
- Each `INPUT` must name an input from the source descriptor.
- Each target must name a declared output with the same service identity.
- Every input is bound exactly once.
- Declarations are resolved as one set, so a target may appear earlier or later
  in the file and cyclic address wiring is valid.
- Arguments after `--` replace the image command arguments. They are
  whitespace-delimited tokens, not a shell command.
- The last provider line is the outer Provider. With no provider lines, the
  gateway is outer.

Declaration order selects the outer endpoint; it does not define startup order.
DComp links are address bindings, not dependency edges.
Standalone `component` directives may coexist in `host.conf`; they are not
Provider declarations and do not participate in outer-Provider selection.

### Bundled pooler

Provider-wide mode takes at least two gateway provider prefixes:

```text
provider pool pooler upstream=gateway.provider -- account-a account-b
```

It preserves the upstream catalogue and adds `pool/LOCAL_MODEL` for each local
model ID advertised by at least two selected providers. Exact-model mode takes
at least two public model IDs and a final output name:

```text
provider pool pooler upstream=gateway.provider -- account-a/model account-b/model model=balanced
```

Members of a virtual model must have compatible inference format,
capabilities, and extensions. The virtual limits are the conservative minima.
The pooler may move an inference request only after typed
`RESOURCE_EXHAUSTED(retry_at)` arrives before the first response. Once any
response has been emitted, and for malformed or ambiguous failures, it
propagates the result without replay. It holds no credentials and treats
payload strings as opaque.

## Runtime links

Cyclo materializes each Provider source as a DComp component and each binding as
a DComp link. For:

```text
provider policy ./policy upstream=trace.provider
```

DComp attaches `policy` and `trace` to a private internal Docker network and
injects:

```text
DCOMP_LINK_UPSTREAM=dns:///trace:50051
```

The target is consumed by the provider's client library. All built-in Cyclo
components use ConnectRPC over HTTP/1.1 TCP. No bearer token, service registry,
sidecar, or host proxy participates in the link.

Each direct link gets a separate network containing only its consumer and
producer. Output fan-out attaches the producer to one network per consumer.
Components receive no addresses for undeclared inputs.

The gateway has external egress for native provider calls. The outer Provider
is published on a dynamic `127.0.0.1` port so the Cyclo host can call
`ListModels` and read the model catalogue. Usage is read separately through a
confined gateway administration container. Intermediate providers are not
published on the host.

## Provider service

The versioned service is:

```proto
service Provider {
  rpc ListModels(ListModelsRequest) returns (ListModelsResponse);
  rpc Infer(InferRequest) returns (stream InferResponse);
}
```

### Catalogue control plane

`ListModels` returns typed model records. A model includes:

- a public `PROVIDER/MODEL` identifier;
- display metadata;
- input/output modalities and capabilities;
- optional context/output limits;
- extension metadata; and
- an `inference_format` ABI identifier.

The provider prefix is a Cyclo route name. The model-local suffix is otherwise
opaque and may contain `/`. Public IDs must be unique in the outer catalogue.
Teams select exact IDs from this catalogue.

Provider components assembling a catalogue must reject or isolate entries whose
inference format they cannot serve. The Pi adapter accepts only the pinned Pi
format and requires the Pi-specific positive token limits. This is endpoint ABI
validation, not inspection of prompts or tool calls.

### Opaque inference data plane

The data-plane messages are deliberately small:

```proto
message InferRequest {
  string model = 1;
  string payload = 2;
}

message InferResponse {
  string payload = 1;
}
```

For the current team runtime:

- the request payload is one JSON string containing Pi's native call frame;
- each response payload is one JSON string containing one native Pi assistant
  event; and
- stream order, event meaning, tool schemas, reasoning, and termination remain
  Pi semantics.

A transparent provider forwards the payload strings exactly. It must not parse,
validate, normalize, or reserialize them. Only the gateway adapter that invokes
the native provider and the Pi adapter inside the team decode these strings.

A component may intentionally inspect payloads to implement policy, auditing,
fusion, or routing. Such inspection is the component's advertised behavior,
not a requirement of the Provider transport.

Cancellation travels through ConnectRPC. `Infer` has no pipeline-wide absolute
deadline: Pi's native-provider timeout must not be converted into a deadline
covering every provider component. The endpoint that invokes the native API
owns that request's network timeout; health and catalogue callers retain their
own bounded deadlines. Routing and transport failures use Connect errors.
Provider/model failures represented by Pi remain opaque Pi events, with one
pre-stream control-plane exception:

- A provider that rejects an unadmitted request for exhausted capacity returns
  `RESOURCE_EXHAUSTED` with a typed
  `cyclo.provider.v1.ResourceExhaustion.retry_at` absolute timestamp.
- This error is valid only before the first streamed response. It explicitly
  means replay through another provider is safe.
- An intermediate provider either handles it by selecting another route or
  propagates it unchanged. If no component handles it, the terminal team
  adapter waits until `retry_at` and retries the identical request.
- Once any response has been streamed, neither the gateway nor an intermediate
  component may convert a later failure into this error or replay the request.

## Health

Every image must define a meaningful OCI `HEALTHCHECK`. DComp observes Docker's
container and health states; it does not require or query a particular health
protocol. Cyclo's built-in components implement their own Component health RPC
and call it from their image health check, but an external provider may use any
bounded probe that accurately represents readiness.

A provider is operational only when its exact DComp component is running and
Docker reports it healthy. Cyclo exposes that fact through:

```sh
cyclo component status NAME
cyclo providers status
cyclo doctor
```

The selected outer component is the only route; a failed component remains
visible and makes the system non-operational.

## Security properties

- Only the gateway receives the credential volume.
- A provider receives inference payloads and link targets only for its declared
  inputs.
- Team and provider components receive no Docker socket or DComp/Cyclo state.
- Link networks are private to their two endpoints.
- Host-side Provider calls are restricted to the DComp-reported loopback port.
- Provider source and Dockerfiles are trusted realm inputs.

This protocol does not provide per-team quotas, semantic filtering, or
confidentiality from a provider in the selected path. Those policies belong in
explicit Provider components or separate realms.

## Implementing a provider

A provider repository should:

1. define its application protobuf services and generated bindings;
2. add `component.dcomp`;
3. build an image that listens on `0.0.0.0:50051`;
4. read each required endpoint from `DCOMP_LINK_<INPUT>`;
5. implement `ListModels` and streaming `Infer`;
6. define a bounded OCI health check; and
7. perform bounded graceful shutdown on Docker's stop signal.

Validate its descriptor and host bindings with:

```sh
cyclo providers check
```

Exercise it independently before installing it in a production `host.conf`.
