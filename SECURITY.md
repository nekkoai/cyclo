# Security policy

## Supported versions

Version 0.2.0 and later stable releases are supported until superseded by a
newer stable release. Security fixes target the latest stable version;
older releases may be asked to upgrade rather than receive a backport.

## Security boundary

A Cyclo installation treats its host operating system, Docker daemon, host
controller, operator-approved configuration, and image build inputs as one
trusted administrative domain. The primary hostile workload is agent-controlled
code inside a team container, including arbitrary execution caused by prompt
injection.

Within that boundary, credentials remain exclusive to the gateway volume;
teams and intermediate providers receive no Docker socket; filesystem access is
limited to declared mounts; and Provider authority is conveyed by explicitly
mounted Unix sockets rather than an internal administrator token. Installed
provider components are trusted with the inference streams and upstream sockets
assigned to them, but not with physical gateway credentials or unrelated
component sockets.

Host compromise is outside this boundary because the host necessarily defines
images, containers, and mounts. Deployments needing a stronger administrative
or kernel boundary should place independent Cyclo installations in separate
operating-system or virtual-machine domains. This is an intentional deployment
boundary, not an assumption that all agent workloads are trusted.

Within one trusted Docker host, separate canonical state roots create separate
Cyclo resource namespaces. Gateway/provider resources, credential volumes,
team containers and networks, default team images, queues, sockets, and
ownership labels are installation-scoped. Lifecycle operations reject a
resource owned by another installation. This prevents accidental cross-instance
adoption; it does not defend against the trusted host or Docker administrator.

Cyclo's component interfaces are designed so additional controls—model policy,
quotas, auditing, filtered egress, or semantic inspection—can be interposed
without moving credentials or Docker authority into team containers. Dashboard
authentication and TLS can likewise be supplied by a trusted reverse proxy.
These are extension points, not claims that every such policy is built in.

The normative threat model, capability semantics, and policy composition points
are documented in [Security architecture](docs/architecture.md#security-architecture).

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, discussion,
or pull request. When available for the published repository, use its private
vulnerability reporting form:

<https://github.com/glguida/cyclo/security/advisories/new>

Cyclo's local release tooling never reads or changes remote repository
settings. If the private form is unavailable, contact the maintainer through a
private channel listed on the publication profile without including exploit or
credential details in a public message.

Include, when possible:

- the affected version or commit;
- impact and the security boundary involved;
- minimal reproduction steps or a proof of concept;
- relevant logs with tokens, API keys, paths, and user data removed; and
- any suggested mitigation or fix.

Use disposable test credentials and accounts. Do not test against systems or
provider accounts you do not own or have explicit permission to assess.

## Response process

The maintainer aims to acknowledge a report within three business days and
provide an initial assessment within seven business days. Fix and disclosure
timing will be coordinated with the reporter and will depend on severity,
provider coordination, and release complexity.

Reports about credential storage, Provider socket capability boundaries,
credential-gateway forwarding, container or mount isolation, filesystem queue
integrity, and unintended dashboard exposure are especially useful. Provider
outages, social engineering, and attacks that require an already-compromised
Docker host are normally outside Cyclo's own security boundary, but reports
showing an unexpected amplification are still welcome.
