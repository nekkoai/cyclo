# Gateway component

This is the credential boundary implemented as the first Cyclo component. It
owns provider credentials, exposes a sanitized model catalogue, and translates
the portable Provider protocol to native model APIs. It does not depend on the
Cyclo CLI or provider runtime.

```text
component gateway
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
```

Both interfaces use ConnectRPC over one HTTP/1.1 Unix socket. The default is
`/run/cyclo/component.sock`; the container publishes no TCP port.

## Security boundary

- Only this container mounts `/var/lib/cyclo-gateway`, which contains
  `auth.json` and `usage.jsonl`.
- Possession of the Unix-socket mount is the complete authority to call
  `Component.Health`, `Provider.ListModels`, and `Provider.Infer`. There is no
  second bearer-token protocol between Cyclo components.
- Incoming RPC authentication metadata is neither interpreted nor forwarded.
  Only credentials resolved from `auth.json` are attached to native provider
  requests.
- Provider keys and OAuth refresh tokens are resolved after request validation
  and never enter protobuf messages, errors, logs, or downstream containers.
- OAuth refreshes use a kernel lock plus atomic replacement.

The public catalogue is an immutable startup snapshot. Login or model-catalogue
changes therefore require a restart; credential values and OAuth refreshes are
read without restarting.

This boundary scrubs literal credential values; it is not DLP. An allowed
provider necessarily receives its own credential and could transform data
before returning it, so exfiltration through that provider is outside the
gateway threat model.

## Files

| Path | Purpose | Access |
| --- | --- | --- |
| `/var/lib/cyclo-gateway/auth.json` | API keys and OAuth credentials | private, writable |
| `/var/lib/cyclo-gateway/usage.jsonl` | per-request token-count audit | private, writable |
| `/etc/cyclo-gateway/models.json` | optional pi custom-provider catalogue | read-only directory mount |
| `/run/cyclo/component.sock` | ConnectRPC socket | producer-owned directory; read-only to consumers |

## Build and test

Run from `src/cyclo/_bundle` in a Cyclo source tree (or from the installed
`cyclo/_bundle` package-data directory). The image consumes the sibling
`component/` and `provider/` interface packages:

```sh
npm --prefix component ci
npm --prefix provider ci
npm --prefix gateway ci
npm --prefix component test
npm --prefix provider test
npm --prefix gateway test
docker build -f gateway/Dockerfile -t cyclo-gateway-component .
```

The same image performs one-shot login against its private state volume:

```sh
docker run --rm -it \
  -v cyclo-gateway-state:/var/lib/cyclo-gateway \
  cyclo-gateway-component login openai-codex

printf '%s\n' "$OPENAI_API_KEY" | docker run --rm -i \
  -v cyclo-gateway-state:/var/lib/cyclo-gateway \
  cyclo-gateway-component login openai --api-key-stdin
```

Before login, `cyclo-gateway-component providers` prints the built-in providers
that have at least one model using a native API implemented by this gateway. It
includes a description, available authentication form, and a copyable login
command. It does not open the credential store.

`cyclo-gateway-component usage` reads `/var/lib/cyclo-gateway/usage.jsonl` and
prints strict JSON totals grouped by account/provider prefix and exact public
model ID. A missing audit file means zero usage. Malformed, oversized, linked,
or numerically overflowing records fail the command instead of silently
producing partial accounting. The report is deliberately global: the shared
tokenless Provider socket carries no trustworthy team identity, so the gateway
does not invent per-team attribution.

The repository's standalone runtime package contains the host lifecycle used to
build, log in, start, stop, restart, and inspect this component. It owns the
labeled credential volume and mounts `/etc/cyclo-gateway` as one read-only
directory plus a stable writable socket directory. Do not bind-mount
`models.json` individually: a file bind pins its inode and can hide a host-side
atomic replacement before restart. The socket directory must be writable by UID
1000 in this container and mounted read-only into consumers. The image
healthcheck calls the generated `Component.Health` client and uses only its exit
status.

## Initial native coverage

The first implementation exposes models using `openai-responses`,
`openai-codex-responses`, and `anthropic-messages`. Text, inline images,
function calls, cancellation, terminal reasons, and usage are translated to the
portable contract. Unsupported controls and history forms fail before
`Started`.

Inline images are limited to signature-checked JPEG, PNG, GIF, and WebP. Tool
schemas use a deliberately bounded JSON Schema subset: typed objects, arrays,
and scalars; properties and required fields; string enums; numeric, length, and
item bounds; descriptions; and boolean `additionalProperties`. The gateway
restores the complete schema after the pinned Anthropic adapter's
canonicalization so no accepted keyword is silently dropped. Completed tool
arguments are still untrusted model output and must be validated by the caller.
Responses-API models reject `is_error` tool results because that native API has
no equivalent; Anthropic preserves them.

The terminal `Finished` event is withheld until its serialized usage record has
been appended. An audit failure therefore fails the RPC and makes health
not-ready instead of silently losing accounting.

Reasoning replay, typed provider-state extensions, `top_p`, stop sequences, and
Google models are intentionally not advertised yet. In particular, the pinned
Google adapter cannot prove a native terminal event on clean EOF, so publishing
it would turn a truncated response into a false success.

The pinned pi adapters preserve stop, output-limit, and tool-call completion,
which this component maps exactly. They currently collapse OpenAI's finer
content-filter/refusal metadata (and Anthropic reports those cases as an
upstream error), so this first component does not emit `CONTENT_FILTER` or
`REFUSAL`. Until the native adapter retains that metadata, callers must treat
those two portable reasons as unavailable from the gateway.
