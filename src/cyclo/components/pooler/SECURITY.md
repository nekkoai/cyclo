# Pooler security boundary

The Cyclo host, Docker daemon, DComp controller, gateway, and installed
Provider components are trusted administrative infrastructure. Model input and
output are untrusted data. A hostile host or Docker daemon is outside this
component's threat model.

The pooler receives inference payloads as opaque strings and forwards them only
in memory. It does not parse, persist, or log payload contents. Routing rewrites
only the public model ID.

The component owns and receives no provider credentials. Account credentials
remain in the gateway's private store; the pooler sees account-qualified public
model IDs and the gateway Provider interface. Do not mount gateway state, Cyclo
state, host credentials, or the Docker socket into its container.

Port 50051 is intended for DComp's private link networks. The Provider protocol
has no public-network authentication layer, so the port must not be published
to an untrusted network.

Failover is permitted only for valid typed `RESOURCE_EXHAUSTED(retry_at)`
received before the first streamed response. Malformed, missing, duplicated,
or ambiguous details are not actionable. Cancellation, deadlines, transport
failures, and other Provider errors are not converted into replay.

Cooldown state is intentionally in memory and resets on restart. The pooler
does not enforce request, token, cost, CPU, memory, or log quotas; deployments
must apply appropriate Docker and host resource controls.
