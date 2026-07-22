# Cyclo components

This directory is the component core. It is intentionally independent of
Docker and of any particular lifecycle manager.

A component is a program with a `component.conf`. A Protobuf service is an
interface, and its fully qualified name is the interface identity. ConnectRPC
generates the HTTP handler and client contract from that service. Cyclo uses
the Connect protocol over HTTP/1.1; gRPC and gRPC-Web are not part of this
component contract.

Components listen on `/run/cyclo/component.sock` by default. A requirement
named `upstream` binds to `/run/cyclo/requirements/upstream/component.sock`.
The producer owns and writes its socket directory; consumers receive that
directory through a read-only mount. The socket is mode `0666` so components
with unrelated non-root UIDs can connect; possession of the private bind mount,
not a shared container UID or a sidecar bearer token, is the access boundary.
Requirement bindings define no credential-file path. This ownership rule is
required because Node removes its Unix-socket
pathname when the server closes. Mount directories rather than socket files so
consumers see the new socket inode after restart.

```text
.proto service
    |
    +-- provide SERVICE       register a complete generated handler
    |
    `-- require NAME SERVICE  construct a generated client
```

Every component explicitly provides `cyclo.component.v1.Component`, whose
single `Health` method distinguishes a reachable process from one that is not
ready. `READY` is operational, `NOT_READY` is reachable but unavailable, and
an unspecified status fails closed. Domain interfaces are separate services.
Interface major versions live in their Protobuf package names; incompatible
changes create `v2`.

## Declaration

```text
component health-proxy
provide cyclo.component.v1.Component
require upstream cyclo.component.v1.Component
```

`require` has a local name because a component may need the same interface
more than once. The declaration contains no URL, socket, container, discovery,
or routing policy. A later assembly layer binds requirement names to component
endpoints.

Unknown directives, duplicate names, malformed interface names, missing base
health, and interfaces absent from the compiled schema fail validation. A
provided interface is also rejected at startup unless every RPC has an
implementation; Connect's permissive unimplemented-method fallback is not used.

## Interface packages

Interfaces are ordinary versioned packages, not entries in a Cyclo registry.
An interface package contains its `.proto` source, generated descriptors, and
`schema.json`. A component imports the generated descriptors it implements or
calls, then validates its declaration against all installed schemas:

```sh
npx cyclo-component-check component.conf \
  node_modules/@cyclo/component/gen/schema.json \
  node_modules/example-domain/gen/schema.json
```

This package exports its base descriptor as `@cyclo/component/contract` and
includes both `proto/` and `gen/` when installed from a local path or tarball.
Domain interfaces use the same layout. The sibling `../provider` package owns
`cyclo.provider.v1.Provider` while using this package for declaration and
binding machinery. The sibling `../gateway` program is the first actual
component built from both interfaces. There is no central runtime discovery
service.

## Build and test

Node.js 20 or newer is required for the current ConnectRPC toolchain.

```sh
npm ci
npm test
npm run check -- test/fixtures/valid.conf gen/schema.json
```

Buf lints the contracts, generates native ESM plus declarations in `gen/`, and
emits `gen/schema.json` for language-neutral declaration validation. The tests
also make a real generated Connect call over a Unix-domain socket.

This package deliberately owns only the common Component interface. Cyclo's
Provider control plane and opaque Pi transport live in the sibling
`../provider` package. It
defines catalogue and inference semantics explicitly rather than disguising a
native model API as `path + headers + bytes` inside Protobuf.
