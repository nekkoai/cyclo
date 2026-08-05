# Cyclo Provider interface

`@cyclo/provider` defines the interface between Cyclo model components. It has
a typed control plane and an opaque inference data plane. ConnectRPC carries
both over HTTP/1.1 TCP on DComp's private link networks.

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
model advertises a `PROVIDER/MODEL` public ID, display metadata, capabilities,
limits, and its `inference_format`. The provider prefix is 1–64 lowercase
letters, numbers, underscores, or hyphens and begins with a letter or number.
The opaque local model portion is 1–1,024 UTF-16 code units, contains no
whitespace, control characters, or unpaired surrogates, and may contain
slashes. Components must
reject incompatible formats when assembling a provider chain. A model
advertising Cyclo's Pi format must include positive
`context_window_tokens` and `max_output_tokens`; the fields stay optional in the
generic wire type so another inference format can define different metadata.
Relays preserve catalogue messages. Pi endpoints isolate and report an
incompatible model instead of rejecting unrelated valid entries.

`Infer` deliberately does not describe prompts, messages, tools, schemas,
reasoning, arguments, or output events. Its request payload is one JSON string
containing a Pi call frame; every response payload is one JSON string containing
a native Pi `AssistantMessageEvent`. Only the Pi endpoints encode or decode
these strings. Relays forward them unchanged.

The package exports the public-ID helpers plus `PI_INFERENCE_FORMAT`,
`encodePayload()`, and `decodePayload()` from `@cyclo/provider/protocol`. The
payload helpers provide JSON framing only. They contain no inference
validation.

Cancellation travels through ConnectRPC, not inside JSON. `Infer` has no
protocol-wide absolute deadline; a provider endpoint owns the timeout for the
native request it performs. Health and catalogue callers use their own bounded
deadlines. Connect errors report routing, transport, dependency, or framing failures.
Provider/model failures represented by Pi remain Pi events in the payload,
except for a request rejected for exhausted capacity before inference starts.

An `Infer` implementation may return `RESOURCE_EXHAUSTED` with a typed
`cyclo.provider.v1.ResourceExhaustion` detail containing an absolute
`retry_at`. This error is valid only before the first streamed response and
means the request was not admitted, so replay through another provider is
safe. Relays propagate it unchanged; a component with another route may try
that route, and the terminal caller waits until `retry_at` only when no route
remains. An error after streaming begins must remain in the Pi event stream and
must never trigger replay.

`@cyclo/provider/errors` exports `createResourceExhaustedError()` and
`resourceExhaustedRetryAt()` so every component constructs and reads the detail
the same way.

An intermediate component declares its graph edges in `component.conf`:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

DComp exposes the selected upstream as
`DCOMP_LINK_UPSTREAM=dns:///provider:50051`. The interface package knows
nothing about Docker credentials, routing policy, or lifecycle.

```sh
npm ci
npm test
```

The tests regenerate the descriptor, exercise real ConnectRPC calls over TCP,
prove exact payload-string preservation, and verify cancellation.
