# Pass-through provider component

This is the smallest intermediate Cyclo provider. It forwards the model
catalogue, opaque inference request, and streamed opaque responses to one named
upstream:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

It listens on `0.0.0.0:50051` and reads the upstream target from
`DCOMP_LINK_UPSTREAM`, for example `dns:///gateway:50051`. Both connections
use ConnectRPC over HTTP/1.1 TCP on DComp-owned private networks.

The pass-through never parses or reserializes `Infer.payload`. Whitespace,
property order, unknown Pi fields, and future events are preserved as strings.
It forwards no caller HTTP headers and owns no bearer, API key, URL, or model
credential. ConnectRPC propagates streaming, backpressure, cancellation,
and transport errors. The pass-through adds no `Infer` deadline; its bounded
deadline is used only while probing the upstream catalogue for health.

`Component.Health` makes a bounded `ListModels` call and reports only `ready`
or a generic dependency failure.

```sh
npm ci
npm test
```

Tests exercise the complete two-component path and assert exact request and
response payload equality, header isolation, cancellation, health recovery,
and shutdown cleanup.
