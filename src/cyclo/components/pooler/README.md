# Cyclo pooler component

The bundled pooler adds quota-aware virtual models to an existing Cyclo
Provider. It selects account-qualified members round-robin and moves to another
member only when the selected member reports the Provider protocol's typed,
pre-stream `RESOURCE_EXHAUSTED(retry_at)` error.

The pooler is an intermediate Provider component. It requires one Provider
input named `upstream`, provides Component health and Provider RPC services,
and holds no credentials.

## `host.conf`

Pool every provider-local model advertised by at least two selected accounts:

```text
provider pool pooler upstream=gateway.provider -- account-a account-b
```

If both accounts advertise `model` and `family/reasoning`, the pooler adds
`pool/model` and `pool/family/reasoning`. All upstream models remain available
under their original IDs.

Pool exact model IDs under one chosen local name:

```text
provider pool pooler upstream=gateway.provider -- account-a/model account-b/model model=balanced
```

That form adds `pool/balanced`. The provider declaration's instance name
(`pool` above) is the output provider prefix. The accepted argument forms are:

```text
PROVIDER PROVIDER [PROVIDER ...]
MEMBER_MODEL MEMBER_MODEL [MEMBER_MODEL ...] model=OUTPUT_MODEL
```

Provider-wide mode accepts any number of distinct provider prefixes; two is the
minimum. It creates a virtual model for every local model ID shared by at least
two selected providers. A selected provider that advertises no models is an
error. Exact mode likewise accepts any number of distinct `PROVIDER/MODEL` IDs
with a minimum of two. The forms cannot be mixed in one instance.

Pool members must advertise identical inference format, capabilities, and
extensions. A virtual model reports the smallest context window and output
limit among its members. Provider-wide models share one provider-level
round-robin cursor and cooldown state; an exact-model pool has its own state.

## Routing guarantees

The pooler retries another member only after typed resource exhaustion arrives
before the first response. It never replays a request after emitting output.
Malformed exhaustion details and all other errors are propagated unchanged. If
every member is cooling down, the pooler returns typed exhaustion with the
earliest retry time; it does not sleep.

Inference payloads remain opaque. The component changes only the model ID on a
pooled upstream request. Cooldowns are held in memory and are reconstructed
from upstream errors after a restart.

## Development

From this directory:

```sh
npm ci --ignore-scripts
npm test
```

From the parent components directory:

```sh
docker build -t cyclo-pooler:dev -f pooler/Dockerfile .
```

The image runs as UID/GID 1000 and exposes only its DComp-managed component
port. Its OCI healthcheck reports catalogue validation failures without leaking
credentials, because the component never receives provider credentials. See
[SECURITY.md](SECURITY.md) for the complete trust boundary.
