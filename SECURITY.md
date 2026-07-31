# Security policy

## Supported versions

Version 0.2.0 and later stable releases are supported until superseded by a
newer stable release. Security fixes target the latest stable version; older
releases may be asked to upgrade instead of receiving a backport.

## Trust model

A Cyclo installation treats these as one trusted administrative domain:

- the host operating system and account running Cyclo;
- the Cyclo package and state root;
- the DComp executable and its state;
- the selected local Docker daemon;
- operator-approved `host.conf`, project files, team repositories, Provider
  sources, Dockerfiles, and images; and
- the gateway implementation and credential store.

The primary hostile workload is arbitrary code inside a team container,
including arbitrary execution caused by prompt injection. Team output, queue
content, model responses, and mounted project content must be treated as
untrusted data by host tooling.

Host or Docker-administrator compromise is outside this boundary: those
principals necessarily control images, containers, networks, mounts, and the
credential volume. Deployments requiring separation from a host administrator
must use distinct OS or VM domains.

## Credential boundary

Physical API keys, OAuth access tokens, and refresh tokens exist only in the
gateway's named Docker volume and gateway process. Cyclo's one-shot login and
usage tools mount that volume only for their bounded administrative operation.

Team and intermediate Provider components receive:

- no gateway credential volume;
- no API key or OAuth session;
- no Docker socket;
- no Cyclo or DComp state directory; and
- no administrator bearer token.

The gateway exchanges authenticated native requests with external model
services. If an upstream response exactly reflects authentication material
inserted by the gateway, the gateway suppresses it and returns a generic
transport failure.

## Component and network boundary

Cyclo compiles one DComp system containing the gateway, configured Provider
components, and desired-running teams. DComp gives each direct interface link a
private internal Docker network containing only its consumer and producer.
Components receive endpoint addresses only for declared inputs.

This is a network capability, not a cryptographic identity. Every endpoint on a
link network is mutually reachable at the network layer. Components must not
expose unrelated services on their interface listener.

The gateway has external egress for native provider calls. The outer Provider
has a loopback-published port for host catalogue operations. Normal teams have
external egress and may publish an AgentWS viewer. `--offline` removes both
direct team egress and viewer publication while preserving the private Provider
link.

An installed Provider is trusted with every inference request and response
explicitly routed through it. It does not receive gateway credentials or
unrelated link networks. Cyclo's transparent Provider transport does not
provide semantic filtering, per-team quotas, or confidentiality from
components on the selected path. Add an explicit policy component or use a
separate installation when those controls are required.

The external model service necessarily receives data sent for inference.
`--offline` prevents direct team egress; it does not make inference local.

## Filesystem boundary

Before emitting DComp binds, Cyclo requires team and project sources to be
canonical existing directories and rejects overlapping authority. Team and
project trees may not overlap:

- one another;
- the Cyclo state root;
- installed Cyclo code;
- the selected `host.conf`;
- the DComp executable;
- the host Pi configuration;
- `/proc`, `/sys`, `/dev`, or `/run`; or
- known Docker socket paths.

Initial launch checks source device/inode identity again after validation.
Every later global apply re-resolves persisted paths and repeats mount-authority
validation.

A project grants exactly the modes it declares:

- `mount NAME PATH rw` exposes `PATH` at `/workspace/NAME`;
- `mount NAME PATH ro` exposes `PATH` at `/readonly/NAME`; and
- `team PATH ro|rw` exposes the selected team at `/team`.

A hostile team can read every declared readable mount and modify every declared
writable mount. A writable team repository intentionally permits
self-modification.

AgentWS code is baked into the image. Only task, job, and agent state
directories are writable binds below `/agentws`; the generated
`/agentws/project.cyclo` is read-only. Pi state is private to one instance.

`cyclo task` uses allowlisted, one-shot AgentWS tools in the immutable team
image. These containers have no network, project/team mounts, Pi state,
credential volume, or Docker socket. Read operations receive only the task
queue read-only; mutations receive only the task queue, plus the job queue for
atomic task creation. The fixed entrypoint drops to the mapped non-root identity
for long-lived teams; one-shot task tools start directly as that same mapped
identity with all capabilities dropped. Task creation reads the requested
specification once without following symlinks, bounds its size, and mounts a
private snapshot from Cyclo state rather than the project path.

## Build boundary

Provider and team Dockerfiles are trusted host programs. Running Cyclo may
execute them through the Docker daemon before any runtime isolation exists.
Review their source, base images, package installation, and build context.

Cyclo runs Docker builds under stable installation/version tags, validates the
completed images, and gives DComp immutable image IDs. Docker owns context
filtering and cache reuse. The immutable ID protects an applied runtime from a
later tag change; it does not make an untrusted build recipe safe or prove
source provenance.

Cyclo refuses to build or run team workloads as host root. Its common team image
maps the invoking UID/GID, starts through a fixed root entrypoint, and drops
privileges before AgentWS starts.

## State and installation boundaries

Cyclo owns domain intent, AgentWS queues, Pi state, and generated system files.
DComp owns lifecycle state below `STATE_ROOT/dcomp` and Docker object
reconciliation. Cyclo communicates with DComp only through machine API version
1 and never parses DComp's private files.

The first operation that needs Docker binds an installation to one canonical
local Unix socket. Later attempts to select another endpoint fail. Remote
Docker endpoints are unsupported.

Different canonical state roots produce different DComp system and Docker
resource names. This prevents accidental cross-installation adoption and name
collision. It is not isolation from the shared host or Docker administrator.

## User interfaces

The fleet dashboard and per-team AgentWS viewers are read-only and
unauthenticated. Their default bind is `127.0.0.1`. Do not expose them on an
untrusted LAN or public network. Use a trusted network boundary or an
authenticated TLS reverse proxy for non-loopback access.

Logs, prompts, model output, project descriptions, and queue files may contain
sensitive project data. Redact them before sharing. Cyclo does not provide an
append-only external log archive; operators may attach ordinary host logging
tools to DComp/Docker output.

## Extension points

The component architecture permits explicit controls without moving credentials
or Docker authority into teams:

- model allowlists and semantic policy;
- request/response audit;
- quotas and accounting;
- pooling or fusion;
- filtered external egress; and
- organization-specific Provider adapters.

An extension receives exactly the Provider traffic and links assigned to it.
Its existence is not itself a security guarantee; its implementation and
configuration remain trusted installation inputs.

## Temporary upstream dependency exception

The 0.2 team runtime includes
`@earendil-works/pi-coding-agent@0.81.1`. Its published npm shrinkwrap pins a
nested `brace-expansion@5.0.7` dependency affected by
[GHSA-mh99-v99m-4gvg](https://github.com/advisories/GHSA-mh99-v99m-4gvg):
a crafted brace expression can exhaust memory and terminate the team process.
This is an accepted workload-availability risk, not an expansion of authority.
It exposes no gateway credentials, Docker control, undeclared mounts, unrelated
Provider links, or other team state. Cyclo already treats agent-controlled code
as arbitrary execution inside the team container; deployments requiring
host-level availability must impose container or VM memory ceilings.

No fixed upstream Pi release was available when this exception was accepted on
2026-07-27. The independently resolvable `pi-lens` copy is fixed at
`brace-expansion@5.0.8`; the audit exception covers only Pi 0.81.1, advisory
GHSA-mh99-v99m-4gvg, version 5.0.7, and the exact nested path
`node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion`.
It covers no other package, path, version, advisory, or critical finding. CI
and the release audit inspect the latest published Pi dependency lock and fail
once a fixed release is available, forcing this exception to be removed and the
aligned Pi dependencies to be updated.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion, or
pull request. When available, use the repository's private vulnerability
reporting form:

<https://github.com/glguida/cyclo/security/advisories/new>

If that form is unavailable, contact the maintainer through a private channel
listed on the publication profile. Do not include credentials, private source,
or exploit details in a public message.

Include:

- the affected version or commit;
- the violated boundary and impact;
- minimal reproduction steps;
- redacted logs; and
- a suggested mitigation when available.

Use disposable accounts and credentials. Do not test against systems or
provider accounts without explicit authorization.

The maintainer aims to acknowledge reports within three business days and
provide an initial assessment within seven business days. Credential handling,
mount/network isolation, DComp ownership checks, Provider transparency, queue
integrity, and unintended dashboard exposure are especially relevant.
