# Cyclo architecture

Cyclo separates five concerns: project authority, team behavior, durable work,
model composition, and credentials. Each has one owner and a small interface.

## System map

```text
project.cyclo
  |
  +-- team repositories -------- roles, roster, optional AGENTS.md
  +-- rw mounts ---------------- /workspace/NAME
  +-- ro mounts ---------------- /readonly/NAME
  +-- container snapshot ------- /agentws/project.cyclo
  |
  v
one team container per selected team
  AgentWS queue + Pi + read-only provider socket mount
  |
  | Provider.Infer(model, opaque Pi JSON)
  v
outer Provider component
  |
  +-- zero or more intermediate Provider components
  |      ConnectRPC over named Unix-socket mounts, network=none
  v
credential gateway (fixed root Provider)
  private credential volume + native Pi adapters + usage audit
  |
  v
external model service
```

The host `cyclo` command reads the component inventory, applies explicit
lifecycle operations, and selects the provider socket given to a team. It is
not a service in the data path.

## Security architecture

### Trust domain and threat model

A Cyclo installation is one host security domain. The host operating system,
Docker daemon, `cyclo` controller, operator-approved configuration, and
operator-approved image build inputs are trusted. This is deliberate: each can
create containers, select images, or change mounts and therefore necessarily
defines the isolation policy it enforces. Compromise of that administrative
domain is not treated as a failure that containers inside the same domain can
contain.

The untrusted workload is agent-controlled code inside a team container. Prompt
injection is assumed capable of arbitrary execution there. Against that
workload, Cyclo's boundary is intended to prevent access to physical provider
credentials, the Docker control plane, undeclared host paths, intermediate
component sockets, and other teams' private AgentWS state. Access deliberately
granted by `project.cyclo`—including shared writable project trees—is authority,
not an isolation failure.

The gateway is trusted with credentials and native upstream traffic. An
operator-installed provider component is trusted with the inference stream and
upstream socket explicitly assigned to it: by definition it can observe,
modify, suppress, or synthesize that traffic. It still receives neither gateway
credentials nor unrelated component sockets. External model providers can see
the data deliberately sent to their models and are outside Cyclo's
confidentiality boundary.

The trusted-host assumption establishes the deployment boundary; it does not
prevent stronger deployment isolation. Mutually distrustful installations can
run under separate operating-system or virtual-machine boundaries. Distinct
Cyclo state roots provide independent installations when several are operated
in one trusted administrative domain.

The canonical state root selects one canonical `components/` state path. Cyclo
hashes that path to a stable 12-hex-character installation ID and scopes all
owned Docker resources with it: gateway/provider containers and images, the
credential volume, team containers and networks, common and derived team
images, and ownership labels. The state root also contains that installation's
queues and Unix sockets. Therefore two installations can reuse logical
project, team, provider, and instance names without sharing mutable Docker
names. A user-selected custom image is explicit shared authority and is not
renamed.

The provider graph belongs to the installation rather than being selected
independently. When the provider graph is first applied, an implicitly selected
state root binds to `/etc/cyclo/host.conf`; a root selected through
`CYCLO_STATE_ROOT` or `--state-root` binds to `host.conf` inside that root. The
binding is persisted under the same state lock as provider lifecycle changes.
Read-only validation and inspection may parse an unbound graph but do not create
state; gateway-only operations neither read nor create the binding. Later
processes honor it even if their environment or spelling of the canonical root
differs; two processes racing cannot apply different provider graphs to one
root.
This is namespace separation inside one trusted Docker host, not a claim of
protection from that host's administrator; mutually distrustful administrators
still require separate OS/VM boundaries.

### Capability model

Cyclo conveys authority through concrete resources rather than a global
administrator credential:

- a project or supporting tree is accessible only when explicitly mounted, at
  its declared `rw` or `ro` mode;
- a Provider edge exists only when the consumer receives that producer's Unix
  socket directory;
- the outer Provider socket authorizes use of its advertised model catalogue;
- the gateway credential volume is mounted only into the gateway; and
- Docker authority remains on the trusted host and is never mounted into a
  team or provider component.

Consequently, possession of a Provider socket is the capability. Cyclo does not
add an ambient bearer or administrator token inside the component graph. The
current outer socket is catalogue-wide rather than a per-model or per-team
identity. A policy requiring distinct principals must therefore issue distinct
socket endpoints or place those principals in separate Cyclo installations;
it must not infer identity from untrusted request metadata.

### Policy composition

The core supplies isolation mechanisms and a transparent inference path. It is
not the single mandatory location for every deployment policy. Additional
controls compose at the boundary that owns the relevant authority:

| Required control | Composition point |
| --- | --- |
| Host or tenant isolation | Separate operating-system or virtual-machine boundary, optionally with a separate Cyclo installation |
| Direct team egress policy | `--offline` today; host/container network policy or a dedicated network boundary for filtered egress |
| Model allowlists, request policy, quotas, or inference audit | An outer Provider component; distinct socket endpoints when policy differs by principal |
| Authentication, TLS, or remote access policy for read-only dashboards | Host firewall or trusted reverse proxy |
| Provider credentials and native upstream authentication | Gateway only |
| CPU, memory, process, filesystem, or storage ceilings | Container and host resource policy |

Because inference payloads are opaque, a relay can remain independent of Pi's
message schema. A security component that needs semantic inspection explicitly
terminates and implements the Pi payload ABI; that decision is local to the
component instead of silently expanding the trusted computing base of every
request.

This separation is also the extensibility criterion for the architecture. A
control is architecturally compatible when it can be added at one of these
boundaries without moving credentials into teams, granting Docker authority to
containers, or changing the transparent Provider transport. Such compatibility
does not imply that the control is already implemented or enabled by default.

## Components and ownership

| Part | Owns | Does not own |
| --- | --- | --- |
| Host controller | Parsing configuration, validating paths, building and inspecting images, mounting sockets/files, starting and stopping owned containers | Prompt semantics, agent job settlement, credentials |
| Team repository | Agent roster, role prompts, optional common instructions | AgentWS implementation, Pi implementation, credentials |
| Team container | One materialized AgentWS runtime, queue processes, Pi state, project mounts | Provider composition, gateway state |
| AgentWS | Durable tasks/jobs/comments/results and the claim-execute-settle loop | Model routing and component lifecycle |
| Pi provider extension | Adapting Pi's in-process stream API to the opaque Provider transport | Credentials and inference validation |
| Intermediate component | One explicitly installed model transformation or routing operation | Sibling sockets, team/project files, gateway state |
| Gateway | Credential store, concrete catalogue, native provider calls, OAuth refresh, usage audit | `host.conf`, projects, teams, intermediate lifecycle |
| Dashboard/viewer | Read-only bounded observations | Queue or lifecycle mutation |

## Component model

The source layout mirrors the component model:

```text
src/cyclo/components/
  protocol/component/  base health and declaration interface
  protocol/provider/   provider catalogue and inference interface
  gateway/             root credential-owning component
  passthrough/         example intermediate component
  pi-provider/         team-side provider adapter
  team-runtime/        common agent workload image
```

Protocol packages define interfaces and do not run independently. The gateway
and intermediate providers are runnable components. The Pi adapter and team
runtime are consumers of the outer Provider interface, not provider
components.

Every component provides the base health interface:

```text
cyclo.component.v1.Component
```

Provider components additionally provide:

```text
cyclo.provider.v1.Provider
```

A repository describes interfaces in `component.conf`:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

The declaration contains no endpoint addresses. The host configuration binds the
named `upstream` requirement to a producer. Cyclo mounts the producer's socket
directory read-only at `/run/cyclo/requirements/upstream`; the component owns
`/run/cyclo/component.sock` in its output directory.

Component containers use their image's immutable entrypoint and healthcheck,
run with a read-only root, private IPC/cgroup namespace, dropped capabilities,
bounded PIDs/file descriptors, a small temporary filesystem, and the exact
socket mounts implied by their declaration. Intermediate components use
`--network none`. No component receives the Docker socket.

All Cyclo-owned containers use one host-side Docker primitive. Cyclo issues
`docker create`, inspects the result, verifies its canonical name, ownership
labels, launch identity, and immutable container ID, and only then issues
`docker start` with that ID. Later stop, removal, log, copy, and exec operations
likewise resolve and verify the resource before addressing it by immutable ID.
The gap between create and start is deliberate: interruption can leave a
stopped, labeled container, but cannot start an unverified one. A later explicit
start, refresh, or repair can identify and remove that exact residue.

The validated image under a component's official tag is the installed
component. Its labels bind it to the installation, component class, and Cyclo
release. `start`, `models`, project `run`, gateway provider discovery, and login
reuse a valid current-release installed image, building only when the tag is
absent or contains an owned image from a different release. Foreign or malformed
tagged images are rejected. Restart requires an already-installed current image
and strictly recreates the container without building.

An explicit build submits the component's normal context to Docker, obtains and
validates the completed image by its immutable image ID, and only then moves the
official tag to it. The build itself needs no temporary Cyclo tag. Build and
refresh are the source-update boundaries:
Cyclo keeps no source hash or cache database, and Docker remains the sole
authority for `.dockerignore` and layer-cache reuse. Runtime status checks that
the container uses the exact installed image ID, plus container ownership,
launch configuration, mounts, isolation, engine health, and the component's
`Health` RPC. Read-only status and doctor commands never build or start
components. “Container running” alone is not readiness.

The host-side implementation has five boundaries:

- `component.py` defines declarations, component records, status records, and
  the base ConnectRPC client;
- `docker_engine.py` is the sole owned-resource mutation, inspection,
  immutable-ID, and create-inspect-start Docker boundary;
- `component_runtime.py` applies component image, mount, isolation, and health
  policy through that boundary;
- `gateway.py` adds only credential-volume and login/catalogue policy; and
- `providers.py` parses `host.conf`, binds Provider requirements, and chooses a
  usable outer Provider socket.

The one read-only exception is selected-endpoint discovery: mount protection
asks `docker context inspect` for the daemon URI with an explicit environment
and timeout before constructing a team container.

`ComponentStatus` describes one component only: its image and container
identity, running/current state, Docker health, Component health, and concrete
inspection error. There is no graph-wide readiness flag and no component
registry service. Inventory is recomputed from the fixed gateway and
`host.conf`.

## Provider system

The installation's `host.conf` is an ordered provider list:

```text
# provider INSTANCE SOURCE [context=PATH] REQUIREMENT=TARGET ... [-- ARGUMENT ...]
provider first ./providers/passthrough upstream=gateway
provider second ./providers/passthrough upstream=first
```

`INSTANCE` is the host-local component name. `SOURCE` contains `Dockerfile` and
`component.conf`. Requirement bindings name `gateway` or an earlier component;
forward references and missing or mismatched interfaces fail before Docker is
called. By default Docker runs the image's declared `ENTRYPOINT` and `CMD`;
Cyclo does not inject or interpret a startup command. Arguments after `--`
explicitly replace the image's OCI `CMD`. Relative paths resolve beside
`host.conf`.

The fixed gateway is always the root. If `host.conf` is absent or empty, the
gateway socket is the provider endpoint. Cyclo examines providers independently
in declaration order and selects the last working component whose declared
inputs are also working. If a component fails to build or start, its dependants
are skipped and an earlier working provider—including the gateway—remains
usable. `models` and project `run` warn; `providers status` prints every
component and exits nonzero when any configured component is not working.
Existing teams retain the endpoint selected when they started and are reported
stale if that selection changes.

Provider route health and catalogue usability are distinct observations.
Before a new team receives a socket, Cyclo asks usable candidates for
`ListModels` from outermost to gateway and selects the first structurally valid
response. This selection remains inference-format-neutral. The team/Pi runtime
boundary then validates the roster's selected models against the pinned Pi ABI.
Fallback is control-plane-only: Cyclo never retries an inference request
through another provider.

Editing the file changes the expected provider list. `providers start`,
`models`, and project `run` start that list from valid current-release installed
images, building only images that are absent or from a different release.
`providers restart` requires current installed images and never builds. Source
edits require an explicit component/provider build or `cyclo refresh`; there is
no watcher or background reconciliation. Individual components can be
inspected and controlled through the same lifecycle:

```sh
cyclo component list
cyclo component status [NAME]
cyclo component build|start|stop|restart|logs NAME
cyclo gateway build|start|restart|stop|status
cyclo providers check|build|start|restart|stop|status
```

Gateway commands add only gateway-specific operations such as login and store
destruction. Provider commands are list-wide conveniences. Both delegate
ordinary image and container work to the component controller.
`component build` and `providers build` update images without restarting them;
`gateway build` rebuilds and restarts the gateway. Global `cyclo refresh`
first builds the selected team images and refreshes the independent gateway
and provider components while teams remain online. It then obtains one
catalogue and validates every selected team's models against it. Only after
those fallible build and compatibility checks succeed does it replace every
team whose recorded intent is `running`.
Inventory, build, stop, and restart execute under one installation lock, so
another host command cannot change the installation halfway through that
operation.

There is no separate refresh journal. Each instance's `run.json` is the durable
record of its desired lifecycle and launch inputs:

- `running` means a matching team container should exist;
- `stopped` means no matching team container should exist; and
- `deleting` means its container, network, and durable AgentWS state are being
  retired.

Docker state is an observation, never a substitute for that intent. Status and
dashboard reads do not rewrite it. `stop` records `stopped` before removing the
exact launch. `forget` records `deleting` before retiring state.
`refresh` leaves `running` intent unchanged while replacing containers.
`repair` makes these records true: it recreates missing, paused, or restarting
`running` instances; verifies AgentWS readiness for every running container
before publishing a recovered port; removes containers belonging to `stopped`
instances; and finishes `deleting` instances. Consequently, interruption can
leave incomplete Docker work but does not invent a second source of lifecycle
truth. Correct the underlying failure and rerun `cyclo refresh` or `cyclo
repair`.

Launch configuration and runtime observations are stored separately. In
particular, the requested AgentWS port (`0` means dynamically assigned) is
reused for recreation; the last published port is only an observation and is
never promoted into launch intent.

Instance metadata and deletion transitions are atomically replaced and synced
before the corresponding destructive Docker or filesystem operation.
Deletion moves the instance tree to `deletions/INSTANCE`, removes its payload
while retaining `run.json`, then atomically moves that metadata to one
`.purged-INSTANCE-LAUNCH.json` marker before removing the empty directory and
marker. Every interruption point is therefore discoverable and exact-launch
retryable; the instance name cannot be reused while either form remains.
For cross-directory renames, Cyclo syncs the destination directory before the
source: a power failure may therefore leave duplicate visible state that fails
closed, but cannot durably remove the source before publishing the destination.
Container ownership is additionally fenced by a random launch ID. A command
that encounters the same name with another installation or launch identity
fails closed instead of adopting or deleting it.

## Provider protocol

Provider control and data use ConnectRPC over HTTP/1.1 Unix sockets.

- `ListModels` is typed because Cyclo and Pi must understand model IDs,
  capabilities, limits, and the inference-format version.
- `Infer` contains only `model` plus an opaque JSON `payload`; every streamed
  response contains only an opaque JSON `payload`.
- ConnectRPC carries cancellation, deadlines, flow control, and transport
  errors outside the payload.

The payload format is Pi's own JSON representation. The team endpoint
serializes `{context, options}` once. Intermediate relays do not parse or
reserialize it. The gateway endpoint parses the call frame once, invokes the
pinned Pi `streamSimple` implementation with the gateway-owned model and
credential, and serializes each native Pi event once. There is no Cyclo message,
tool, schema, reasoning, argument, or event model.

The gateway rejects only invalid framing at this boundary. It does not validate
inference contents. Credential and process controls remain out of band:
`apiKey`, arbitrary headers/environment, callback/client objects, the abort
signal, and native transport/timeout/retry controls cannot be supplied through
the payload. All other JSON Pi options pass without a Cyclo allowlist. Before a
native event leaves the credential boundary, the gateway makes one
schema-independent containment check over its serialized JSON strings and
property names. An event that exactly reflects gateway-injected authentication
material is suppressed and becomes a generic transport error; every other
event remains unchanged.

The normative details are in [Provider protocol v1](provider-protocol.md).

## Gateway boundary

The gateway is an independent root component. Its Docker volume contains
credentials, OAuth sessions, and the usage ledger. No team or intermediate
component mounts that volume. The public model catalogue exposes account/model
names and safe capabilities, never native headers, base URLs, or credentials.

Host-only gateway commands run in labeled one-shot containers under the
installation's kernel lock. If the controlling process is killed or the host
restarts, the lock is released automatically; the next locked gateway
operation verifies the abandoned container's installation labels, canonical
name, and immutable Docker ID before removing the container without removing
the credential volume. A concurrent dashboard request never waits behind an
interactive login: usage is reported as temporarily unavailable instead.
Dashboard observation neither creates an absent installation nor binds its
provider-configuration scope.

`cyclo gateway login` constructs the candidate credential document under the
gateway's kernel file lock and validates it through the same configured
catalogue path before atomically replacing the private store. An unknown or
misconfigured provider therefore leaves the previous store and running gateway
intact. The long-running gateway reads credential values dynamically, while its
model catalogue is a startup snapshot. Successful login restarts the gateway
automatically and returns only after the updated catalogue is ready. OAuth
refreshes use the same kernel lock and atomic file replacement. Gateway health
also compares that snapshot with the current non-secret catalogue. If the host
is killed after the store commit but before restart, the old process becomes
not-ready; the next ordinary start or model operation replaces it and publishes
the committed catalogue.

Incoming ConnectRPC headers are not forwarded to native services. The gateway
chooses the native model from its catalogue, resolves the matching credential,
overrides credential/transport controls, and records observed Pi usage. Usage
observation does not mutate or reorder the response payload stream. The
append-only ledger treats newline as the record commit boundary, repairs an
incomplete crash tail at startup, and remains unhealthy after an append error
until restart rather than writing behind an uncertain tail.

## Projects and teams

`project.cyclo` is the complete authority for a run:

```text
name core-et-uart
description Integrate and verify a reusable UART IP in CORE-ET.
context <<PROJECT_CONTEXT
`core-et` is the processor repository; `uart-ip` is a separate project being
integrated into it; `specifications` is normative input for both.
PROJECT_CONTEXT
team ../teams/jon-rtl ro
team ../teams/rtl-auditor ro
mount core-et ../sources/core-et rw
mount uart-ip ../sources/uart-ip rw
mount specifications ../references/specifications ro
```

Cyclo starts one independent instance per team. Every `rw` mount is a writable
project at `/workspace/NAME`, so one run may intentionally expose several
projects. A `ro` mount is supporting input at `/readonly/NAME`. Cyclo does not
infer a primary project; the authored description and context explain what
each project is and how the mounted trees relate. Team mode controls whether
`/team` itself is writable. Relative paths resolve beside the definition. All
selected trees must be real, non-overlapping directories.

Before the first container starts, Cyclo validates every team, requested model,
mount, provider connection, and bind-source identity. Each team launch is an
independent operation: a failed launch removes its own exact container, while
teams already started by the same project command remain running and visible.
Queue history remains under the state root. First-instance metadata is built
outside the authoritative inventory and published as one complete directory
before runtime materialization, so interruption exposes either no instance or
a valid launch-pinned record that `cyclo repair` can reconcile.
Retirement makes the inverse transition: it first persists `deleting` intent,
then renames the complete instance out of authoritative inventory and
recursively removes the inert tree. Interruption therefore cannot leave an
inventory entry without `run.json`, and `cyclo repair` can finish either side
of the rename.

Restarting an instance with a stopped or dead container uses two launch
identities rather than adopting the old container. Cyclo verifies and removes
the exact persisted old launch while its metadata is still authoritative, then
atomically publishes the replacement launch before creating its container.
An interruption therefore leaves either the old record with no old container,
or the new record with no old container; both states remain inspectable and
repairable. A running, paused, restarting, foreign, or differently labeled
container is never removed by this replacement path.

A team repository contains its behavioral definition and optional execution
delta:

```text
team
roles/*.md
AGENTS.md          # optional
Dockerfile         # optional; ARG/FROM the Cyclo team-runtime base
```

The roster line format is:

```text
AGENT ROLE ENGINE PROVIDER/MODEL
```

Cyclo supplies the common AgentWS runtime and Pi extension in the team image.
It generates one `/agentws/project.cyclo` per running team instance. The
snapshot uses the same grammar as the host definition, keeps its authored
name, description, and context, selects only that instance's team at `/team`,
and replaces structured host paths with `/workspace/NAME` or
`/readonly/NAME`. The runtime tree and snapshot are mounted read-only, and the
generic `AGENTS.md` requires every agent to read that fixed project contract.
Its contents are not duplicated into tasks or job prompts. Generated `team`
and `mount` fields never expose host paths; authored description/context text
is copied literally.

A repository Dockerfile must declare `ARG CYCLO_TEAM_BASE` before its first
`FROM`; its final stage must inherit that exact base. Earlier builder stages
may use other images. Cyclo labels the derived image with the exact base image
ID, validates the completed image by immutable ID and the inherited runtime
entrypoint, requires that entrypoint to start as root so it can create and drop
to the host-mapped runtime identity, and promotes the team tag only after
success. A normal project run always asks Docker to build the selected context;
Docker reuses cached layers when appropriate. Teams without a Dockerfile use the
common image directly.

## Team isolation and state

Team containers mount:

- the selected team at `/team` with its declared mode;
- writable projects below `/workspace`;
- read-only inputs below `/readonly`;
- a read-only materialized AgentWS runtime;
- writable queue and Pi state owned by that instance; and
- the final Provider socket directory read-only.

The provider socket is authority to use the configured model catalogue; no API
key or subscription session enters the team. `--offline` starts the team with
Docker's `--network none`, while leaving the mounted Unix provider socket
available.

Persistent state defaults to `$XDG_STATE_HOME/cyclo` or
`~/.local/state/cyclo`:

```text
instances/INSTANCE/
  agentws-state/          tasks, jobs, agents, comments, and results
  pi/                     Pi settings and runtime metadata
  runtime/                generated read-only AgentWS runtime
  run.json                launch inputs and running/stopped/deleting intent
deletions/INSTANCE/       transient state for a retryable explicit deletion
deletions/.purged-INSTANCE-LAUNCH.json
                           final retry marker; normally too brief to observe
components/gateway/socket/       root component socket
components/sockets/COMPONENT/    intermediate component sockets
```

Physical credentials and the usage ledger live instead in a separately owned
Docker volume. `cyclo gateway status` prints its installation-specific name.
Ordinary stop/restart operations do not delete it; `cyclo gateway
destroy-store --confirm VOLUME` is the explicit destructive operation.

New team resources use `cyclo-SYSTEM-team-INSTANCE` containers and
`cyclo-SYSTEM-team-INSTANCE-net` networks. The common image is
`cyclo-SYSTEM-team:VERSION`; derived images add a team name and repository-path
digest. `SYSTEM` is derived from the state root. Labels record the same system,
resource kind, and logical instance, so a name alone is never sufficient
authority for lifecycle operations. Operations on a persisted current instance
also require its `cyclo.launch` identity and target the immutable inspected
container ID. Startup may inspect a stopped previous launch by ownership so it
can replace it, but does not adopt that launch as current.

## Failure model

- AgentWS settles engine attempts from durable queue state and bounded retry
  rules. Provider health does not retroactively reinterpret an agent exit.
- A component works only when its exact current container is running, its
  Docker healthcheck is healthy, and its own health RPC reports ready.
- Provider dependency checks decide startup order and route selection; they do
  not rewrite one component's status into an aggregate graph status.
- Unknown models fail before native dispatch.
- Connect failures remain transport failures; provider failures already emitted
  as Pi events remain Pi events.
- Configuration is declarative and command-applied. Mutating lifecycle commands
  apply the selected provider list; observational commands report component
  facts without changing it.

`cyclo doctor` checks the installed AgentWS/component ABI, persisted state,
Docker, host configuration, gateway, every intermediate component, and the outer
catalogue without changing the system.
