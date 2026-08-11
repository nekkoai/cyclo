# OpenAI edge component

This is a standalone terminal component. It exposes an OpenAI-compatible HTTP
surface and consumes exactly one Cyclo Provider:

```text
OpenAI client ──HTTP──> openai component ──Provider/ConnectRPC──> provider
```

It is deliberately not part of `team/pi`. The team adapter translates in the
other direction inside a Pi process; this package has its own process,
declaration, image, health service, lifecycle, and tests.

The HTTP listener implements the stateless Responses subset:

```text
GET  /v1/models
GET  /v1/models/{model}
POST /v1/responses
```

Both streaming and non-streaming requests use the pinned Pi call-frame and
event ABI carried by `cyclo.provider.v1.Provider`. The adapter translates text,
base64 data-URL image input, assistant history, reasoning summaries, function
definitions, function calls and outputs, usage, errors, and cancellation.

Stored conversations, `previous_response_id`, background execution, hosted
tools, remote/file image fetching, structured output, and parameters that
cannot be represented faithfully by the Pi ABI are rejected rather than
silently changed. HTTP authorization headers never enter the Provider payload.

The component listens on two ports:

- `50051`: `cyclo.component.v1.Component` health service.
- `8080`: OpenAI HTTP API, configurable with `CYCLO_OPENAI_HOST` and
  `CYCLO_OPENAI_PORT`.

It reads its required Provider target from `DCOMP_LINK_PROVIDER`, for example
`dns:///gateway:50051`. Set `CYCLO_OPENAI_API_KEY` to require the same bearer
token from HTTP clients. Without it, authentication is intentionally disabled;
the listener should then remain on a private component network or be published
only to loopback.

```sh
DCOMP_LINK_PROVIDER=dns:///gateway:50051 \
  CYCLO_OPENAI_API_KEY=local-secret \
  node src/main.mjs
```

An OpenAI SDK client can use `http://127.0.0.1:8080/v1` as its `baseURL`. Model
IDs are the Provider catalogue's public `PROVIDER/MODEL` IDs.

This is an HTTP edge, not another Provider in the host's Provider chain, so it
does not declare a Provider output or live in a provider-source
`component.dcomp`. Enable it for a Cyclo installation in `host.conf`:

```text
component openai
```

Cyclo links its `provider` input to the final Provider output and publishes
container port `8080` as `127.0.0.1:8080` by default. Use
`component openai bind=0.0.0.0 port=18080` to select another literal host IPv4
address and port, then run `cyclo repair`. The `bind` setting controls Docker's
host publication, not `CYCLO_OPENAI_HOST`, which controls the listener inside
the component. A wildcard host publication exposes the API on every IPv4
interface and should be used only behind an appropriate trusted network
boundary.

```sh
npm ci
npm test
```
