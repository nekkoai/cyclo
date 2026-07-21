# Pass-through provider component

This is the smallest intermediate Cyclo provider. It forwards a live model
catalogue and inference stream without aliases, filtering, caching, retries, or
request translation:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

It serves ConnectRPC over `/run/cyclo/component.sock` and connects to its
upstream at `/run/cyclo/requirements/upstream/component.sock`. Both are Unix
sockets; the image needs no network. A producer owns its socket directory and
consumers mount it read-only. Mount directories, not individual socket files,
so a restarted component can replace its socket inode.

The private upstream socket mount is the entire authority for this edge. The
component reads no bearer file, accepts no upstream credential through the
environment, and forwards none of the caller's headers. Environment variables
may override only the two socket paths:

```text
CYCLO_COMPONENT_SOCKET
CYCLO_REQUIRE_UPSTREAM_SOCKET
```

`Component.Health` makes a bounded `ListModels` call. It reports
only `ready` or the generic `upstream provider unavailable`; dependency failures
do not stop the process, so health can recover when the upstream returns.

`Infer` preserves protobuf semantics and streams incrementally. It does not
promise byte-for-byte identity of the Connect HTTP envelope, response headers,
or trailers. The shared Provider validator withholds `Finished` until upstream
EOF, rejects malformed or truncated streams as `DATA_LOSS`, and propagates
explicit Connect errors and cancellation without retrying.

A pass-through instance has no bearer identity to delegate. If a future system
needs per-caller attribution across an intermediate provider, that must be an
explicit protocol rather than forwarded ambient credentials.

## Build and test

Run from `src/cyclo/_bundle` in a Cyclo source tree (or from the installed
`cyclo/_bundle` package-data directory):

```sh
npm --prefix component ci
npm --prefix provider ci
npm --prefix passthrough ci
npm --prefix component test
npm --prefix provider test
npm --prefix passthrough test
docker build -f passthrough/Dockerfile -t cyclo-passthrough-component .
```

The intended container posture is non-root and read-only, with no TCP network:

```sh
docker run --rm --read-only --network none \
  --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,src=/host/upstream,dst=/run/cyclo/requirements/upstream,readonly \
  --mount type=bind,src=/host/output,dst=/run/cyclo \
  cyclo-passthrough-component
```

The runtime mounts only the selected producer's socket directory into the
consumer. Possession of that private mount is the sole edge capability.
