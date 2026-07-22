# Credential gateway component

The gateway is Cyclo's fixed root Provider and credential boundary. It owns
accounts, API keys, OAuth refresh state, the concrete Pi model catalogue,
native provider calls, and usage accounting. It does not parse `host.conf` or
manage intermediate components.

```text
component gateway
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
```

Both services use ConnectRPC on `/run/cyclo/component.sock`. The gateway has no
TCP listener. Only its container mounts `/var/lib/cyclo-gateway`, containing
`auth.json` and `usage.jsonl`.

## Inference boundary

Cyclo routes an `Infer` request using only its outer `model` field. The gateway
then resolves the corresponding account credential and passes the opaque
payload to its Pi endpoint adapter.

That adapter parses only the Pi call frame (`context` plus `options`) and calls
the pinned `pi-ai` `streamSimple` implementation with the gateway-owned native
model and credential. It does not interpret or validate prompt content,
history, tools, JSON Schema, reasoning, tool arguments, or returned Pi events.
Every native Pi event is serialized immediately into one response payload.

The caller cannot choose credentials or gateway process controls. `apiKey`,
arbitrary headers/environment, injected clients, callbacks, and the abort
signal are gateway-owned; all other JSON Pi options pass without a Cyclo
allowlist. ConnectRPC carries cancellation out of band.

Incoming RPC headers are never forwarded. Provider credentials never appear in
the model catalogue, request payload, Docker arguments, logs, or downstream
containers. The public catalogue is a startup snapshot; login or model changes
therefore require a gateway restart. Credential values and OAuth refreshes are
read dynamically and written with kernel locking plus atomic replacement.

Usage is observed at the native Pi endpoint and appended to the private audit
file. Accounting observes terminal event usage but does not alter or reorder
the payload stream. A client-abandoned or failed stream is recorded with its
transport outcome.

## Files

| Path | Purpose | Access |
| --- | --- | --- |
| `/var/lib/cyclo-gateway/auth.json` | API keys and OAuth credentials | private, writable |
| `/var/lib/cyclo-gateway/usage.jsonl` | request/token audit | private, writable |
| `/etc/cyclo-gateway/models.json` | optional custom Pi catalogue | read-only |
| `/run/cyclo/component.sock` | Component and Provider ConnectRPC | producer-owned socket |

## Build and test

From `src/cyclo/components`:

```sh
npm --prefix component ci
npm --prefix provider ci
npm --prefix gateway ci
npm --prefix component test
npm --prefix provider test
npm --prefix gateway test
docker build -f gateway/Dockerfile -t cyclo-gateway-component .
```

The image also performs one-shot login and usage commands against its private
state volume. `providers` needs no credential store; `login` writes it; `usage`
reads the audit without exposing credentials.
