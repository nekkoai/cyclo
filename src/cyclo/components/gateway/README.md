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
The one gateway-specific egress check is schema-independent: if a serialized
event exactly reflects an API key or authentication-header value injected by
the gateway, the event is discarded and inference fails with a generic
`DATA_LOSS` error. Events without gateway authentication material remain
unchanged.

The caller cannot choose credentials or gateway process controls. `apiKey`,
arbitrary headers/environment, injected clients, callbacks, and the abort
signal are gateway-owned; all other JSON Pi options pass without a Cyclo
allowlist. ConnectRPC carries cancellation out of band.

Incoming RPC headers are never forwarded. Provider credentials never appear in
the model catalogue, request payload, Docker arguments, logs, or downstream
containers. The public catalogue is a startup snapshot; login or model changes
therefore require a gateway restart, which `cyclo gateway login` performs
automatically. Health detects when the committed non-secret catalogue differs
from that snapshot, so an interrupted post-login restart is repaired by the
next ordinary start or model operation. Login builds the candidate catalogue in
memory before atomically replacing `auth.json`; an unknown provider or unusable
custom catalogue leaves the previous credential store and running gateway
untouched. Credential values and OAuth refreshes are read dynamically and
written with kernel locking plus atomic replacement.

Models using the Pi inference format must publish positive context-window and
output-token limits. The gateway excludes an unusable model without hiding
valid models from the same account, and reports a bounded safe reason at
startup logs. The team-side Pi adapter repeats this check because an
intermediate provider may supply its own catalogue; it logs and ignores only
the bad entry. Intermediate relays preserve the typed catalogue fields
unchanged.

Usage is observed at the native Pi endpoint and appended to the private audit
file. Accounting observes terminal event usage but does not alter or reorder
the payload stream. A client-abandoned or failed stream is recorded with its
transport outcome. Records are newline-committed: startup truncates only an
incomplete crash tail, and a write failure keeps health not-ready until that
restart repair has run.

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
npm --prefix protocol/component ci
npm --prefix protocol/provider ci
npm --prefix gateway ci
npm --prefix protocol/component test
npm --prefix protocol/provider test
npm --prefix gateway test
docker build -f gateway/Dockerfile -t cyclo-gateway-component .
```

The image also performs one-shot login and usage commands against its private
state volume. `providers` needs no credential store; `login` writes it; `usage`
requires the existing store and reads the audit without exposing credentials.
The host labels these command containers and removes a verified abandoned
command after controller or host failure; the named credential volume is never
created or removed by usage cleanup.
