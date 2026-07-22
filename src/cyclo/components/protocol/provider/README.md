# Cyclo Provider interface

`@cyclo/provider` defines the interface between Cyclo model components. It has
a typed control plane and an opaque inference data plane. ConnectRPC carries
both over HTTP/1.1 Unix-domain sockets.

```proto
service Provider {
  rpc ListModels(ListModelsRequest) returns (ListModelsResponse);
  rpc Infer(InferRequest) returns (stream InferResponse);
}

message InferRequest {
  string model = 1;
  string payload = 2;
}

message InferResponse {
  string payload = 1;
}
```

`ListModels` is typed because Cyclo must assemble and expose a catalogue. Each
model advertises an opaque ID, display metadata, capabilities, limits, and its
`inference_format`. Components must reject incompatible formats when assembling
a stack.

`Infer` deliberately does not describe prompts, messages, tools, schemas,
reasoning, arguments, or output events. Its request payload is one JSON string
containing a Pi call frame; every response payload is one JSON string containing
a native Pi `AssistantMessageEvent`. Only the Pi endpoints encode or decode
these strings. Relays forward them unchanged.

The package exports `PI_INFERENCE_FORMAT`, `encodePayload()`, and
`decodePayload()` from `@cyclo/provider/protocol`. Those helpers provide JSON
framing only. They contain no inference validation.

Cancellation and deadlines travel through ConnectRPC, not inside JSON.
Connect errors report routing, transport, dependency, or framing failures.
Provider/model failures represented by Pi remain Pi events in the payload.

An intermediate component declares its graph edges in `component.conf`:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

The component runtime mounts the selected upstream socket at the named
requirement path. The interface package knows nothing about Docker, paths,
credentials, routing policy, or lifecycle.

```sh
npm ci
npm test
```

The tests regenerate the descriptor, exercise real ConnectRPC calls over Unix
sockets, prove exact payload-string preservation, and verify cancellation.
