# Architecture

## Purpose

Cyclo turns four kinds of operator-owned input into a running agentic system:

- gateway credentials;
- a host component configuration;
- Git-defined teams; and
- project definitions with explicit filesystem authority.

Cyclo is a host CLI and compiler. It is not a daemon, proxy, scheduler, or
container lifecycle engine. DComp is the sole lifecycle engine. It validates
and reconciles the Docker components, direct interface links, networks,
volumes, and interrupted operations described by Cyclo.

After startup, inference travels directly between components. Neither the Cyclo
CLI nor DComp is in that data path.

## Components

One Cyclo realm contains these runtime component classes:

| Component | Responsibility | Persistent authority |
| --- | --- | --- |
| Gateway | Provider login, credential refresh, native upstream calls, model catalogue, usage ledger | Gateway credential volume |
| Intermediate Provider | Transform, route, filter, pool, or observe Provider traffic | Only its declared volumes, if any |
| OpenAI edge | Standalone OpenAI Responses HTTP endpoint consuming one Provider | None |
| Team | AgentWS workers, Pi, project tools, read-only AgentWS viewer | Its queue and Pi directories |

The OpenAI edge is independently deployable and is not embedded in a team. It
is added only when `host.conf` contains `component openai`. Cyclo then links
its Provider input to the outer Provider and explicitly publishes its HTTP port
on the configured host IPv4 address, which defaults to loopback.

The host also runs two short-lived programs:

| Program | Responsibility |
| --- | --- |
| `cyclo` | Parse domain configuration, build images, persist instance intent, compile the global DComp system, invoke administrative tools |
| `dcomp` | Validate and apply that system, own Docker lifecycle state, report component/network status, recover interrupted operations |

DComp is an external executable. Cyclo discovers it through `CYCLO_DCOMP` or
`PATH`, requires machine API version 1, and gives it
`STATE_ROOT/dcomp` as its realm-scoped state directory.
Docker resource names owned by DComp are opaque to Cyclo. Administrative code
resolves the gateway's declared `credentials` volume through
`dcomp volume --json`; it does not reproduce DComp's naming rules.

## One realm-wide system

The canonical Cyclo state root produces a stable realm identifier and a single
DComp system named `cyclo-<realm-id>`. Every apply compiles:

1. the fixed gateway;
2. every Provider component declared by the selected `host.conf`;
3. every standalone component declared by `host.conf`; and
4. every persisted team instance whose intent is `running`.

All active teams therefore share one declared provider stack but retain
independent component containers, queues, Pi state, project mounts, and
dashboard ports.

```text
                           host loopback
                                │
                         model discovery
                                │
                                v
┌─────────────┐   private   ┌──────────┐   private   ┌──────────┐
│   gateway   │────────────>│ provider │────────────>│ provider │
│ credentials │    link     │    A     │    link     │ outer    │
└─────────────┘              └──────────┘              └────┬─────┘
                                                           │
                          ┌────────────────────────────────┼──────────────┐
                          │ one private link/team           │              │
                          v                                 v              v
                     ┌────────┐                        ┌────────┐    ┌──────────┐
                     │ team 1 │                        │ team 2 │    │ OpenAI   │
                     └────────┘                        └────────┘    │ HTTP edge│
                                                                  └────┬─────┘
                                                                       │
                                                    configured IPv4:port
                                                   (127.0.0.1:8080 default)
```

Arrows represent interface address bindings, not lifecycle dependencies.
DComp permits fan-out and cycles. Cyclo does not infer a startup order or route
from graph shape.

## Configuration layers

No file is a universal configuration database.

### Host configuration

`host.conf` installs intermediate Provider components and enables bundled
terminal host components:

```text
provider NAME SOURCE [context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARGUMENT ...]
component openai [bind=IPV4] [port=PORT]
```

The default realm combines `/etc/cyclo/host.conf` with state in
`/var/lib/cyclo`. `--local` selects a self-contained private XDG realm for the
current user. It, an explicit `--state-root`, and `CYCLO_STATE_ROOT` all read
`STATE_ROOT/host.conf`; therefore configuration, gateway logins, teams, and
runtime state stay in one realm.

Host configuration selects the Provider and standalone-component plane. Team
definitions and running/stopped intent are stored under the same realm's
`instances/` tree, not in `host.conf`. Gateway accounts and usage live in that
realm's gateway volume. Thus changing to a different `host.conf` necessarily
changes the state root, gateway logins, teams, and DComp system.

The gateway is implicit and always exposes `gateway.provider`. An empty file
uses it directly. Every provider input must be bound exactly once. All
declarations are collected before bindings are resolved, so a target may be
declared earlier or later. The last provider declaration is the outer Provider.

`pooler` is the packaged Provider source token. Cyclo resolves it
inside the installed components build root; provider-wide arguments create one
virtual model for each local ID shared by selected account prefixes, while
exact-model arguments create one named virtual model. The component remains in
the ordinary Provider chain and receives no credentials.

`component openai` does not add another Provider to that chain. It adds one
terminal HTTP component, links its `provider` input to the outer Provider, and
publishes container port 8080 as `127.0.0.1:8080` by default. The optional
`bind` setting selects a literal host IPv4 address, and `port` selects the host
port. Setting `bind=0.0.0.0` makes the API reachable on every host IPv4
interface and therefore requires an appropriate trusted network boundary.

### Component descriptor

Each provider source contains `component.dcomp`:

```text
docker example/provider:1
input cyclo.provider.v1.Provider upstream
output cyclo.provider.v1.Provider provider
```

The descriptor declares nominal protobuf service identities and local endpoint
names. Cyclo requires each provider to expose exactly one
`cyclo.provider.v1.Provider` output. DComp validates the generated system
against the same endpoint contract.

If the source contains a `Dockerfile`, Cyclo builds it. `context=PATH` may
select a containing build context; otherwise the source directory is the
context. Without a Dockerfile, the image named by `docker` must already exist
and define an OCI health check.

### Team repository

A team repository supplies a roster, `roles/*.md`, optional `AGENTS.md`, and a
required Dockerfile derived from `CYCLO_TEAM_BASE`. It contains behavior and
execution dependencies, not credentials or durable queue state. Cyclo builds
each normal team image from this repository over its standard team-component
image.

The roster assigns each agent one role:

```text
NAME ROLE ENGINE PROVIDER/MODEL
```

AgentWS jobs carry `ROLE`; workers claim only jobs matching the role in their
roster entry, and `roles/ROLE.md` supplies their behavioral instructions.
Multiple agents may share one role. Task-creation authority and automatic
planner-notification suppression belong to role `planner`.

### Project definition

`project.cyclo` selects one or more teams and explicitly grants access to named
host directories. It also supplies a name, description, and optional literal
context describing the mounted projects.

## Builds and image identity

Cyclo owns image construction; DComp deliberately does not build or pull
images. Cyclo uses stable tags scoped by realm, component kind, Cyclo
version, and—where necessary—Provider or team identity.

Whenever an operation needs a built host or team image, Cyclo invokes
`docker build` with that stable tag. It does not hash source trees, pre-judge
whether a tag is current, emulate `.dockerignore`, or maintain build history.
Docker receives the real context and owns file selection and layer-cache reuse.
Build output is captured rather than streamed and surfaced on failure.

Cyclo asks Docker to write the built image ID, inspects the completed stable
tag, verifies both identities agree, and passes only the immutable `sha256:`
image ID to the generated DComp definition. A single CLI operation may retain
that inspected result in memory to avoid asking for the same shared base twice;
there is no persistent Cyclo image cache.

Team images must preserve:

- Cyclo's fixed entrypoint;
- the OCI health check; and
- a root final image user so the entrypoint can assume the host UID/GID and
  then drop privileges.

Cyclo writes the current components as content-addressed descriptor directories
below `STATE_ROOT/system/descriptors/`, atomically replaces
`STATE_ROOT/system/system.dcomp`, and removes descriptors that the selected
file no longer references. It keeps no history of generated systems. Cyclo
validates the selected file with `dcomp check` and invokes `dcomp up`. DComp
resumes an unfinished matching target or safely supersedes it when the newly
resolved target differs.

DComp retains unchanged component instances and replaces only components whose
resolved definition changed.

## Interfaces and networking

Every Cyclo application interface is identified by a fully qualified protobuf
service name. DComp checks nominal equality between linked input and output
declarations. It does not inspect protobuf descriptors or application payloads.

Cyclo's built-in components use ConnectRPC over HTTP/1.1 TCP and listen on
`0.0.0.0:50051`. For a link such as:

```text
link policy.upstream trace.provider
```

DComp creates a private internal Docker network containing those two
components and injects:

```text
DCOMP_LINK_UPSTREAM=dns:///trace:50051
```

Each direct link has its own network. A consumer receives only targets for its
declared inputs. No service registry, proxy, sidecar, bearer token, credential
file, or Docker socket is needed.

External routing is explicit:

- the gateway has egress for native provider calls;
- the outer Provider has an externally routed base network because Cyclo
  publishes its Provider port on a dynamic loopback port for host-side calls;
- a normal team has egress and may publish its AgentWS viewer;
- `--offline` removes team egress and viewer publication while preserving its
  private Provider link; and
- non-outer intermediate providers have no external base network unless the
  generated system explicitly grants one.

The host-side Provider client accepts only `127.0.0.1` and obtains the current
dynamic port from DComp status.

## Provider data plane

`cyclo.provider.v1.Provider` has two RPCs:

- `ListModels`, a typed catalogue control plane; and
- `Infer`, a streaming opaque data plane.

`InferRequest` contains an exact public model ID and one string payload.
`InferResponse` contains one string payload. For the current Pi integration,
those strings contain Pi JSON frames and events. Relays forward them without
interpreting, validating, or reserializing them. A policy component may
deliberately inspect them, but transparency is the base transport contract.

The gateway is the only component that receives physical credentials.
Intermediate providers can observe the inference data explicitly routed
through them, but not credential files or unrelated links.

## Team runtime

The common team image contains:

- AgentWS code and generic `AGENTS.md`;
- the Cyclo team supervisor;
- Pi and the Cyclo Provider adapter;
- the read-only AgentWS viewer; and
- the standard command-line tools.

The source tree follows that runtime boundary. `cyclo.components.team` owns
everything copied into or executed by the team image: its Dockerfile,
entrypoint, supervisor, AgentWS tree, Pi adapter, and JavaScript dependencies.
`cyclo.team` is the separate host-side library for team definitions, packaged
templates, image construction, DComp component compilation, queue inspection,
compatibility checks, and confined task administration. Shared Component and
Provider interface packages remain under `cyclo.components.protocol`; the Pi
adapter is not an independent runtime component.

AgentWS code is image content, not a host bind mount. A running team receives:

```text
/agentws/tasks             durable writable task state
/agentws/jobs              durable writable job state
/agentws/agents            durable writable agent state
/agentws/project.cyclo     generated read-only project view
/opt/cyclo/pi-settings.json generated read-only Pi settings template
/home/cyclo/.pi            realm-scoped writable Pi state
/team                      team repository, ro or rw
/workspace/<name>          each rw project mount
/readonly/<name>           each ro supporting mount
```

The supervisor reads `/agentws/project.cyclo`, recovers orphaned queue work,
starts AgentWS workers and the viewer, and performs bounded child-process
shutdown. DComp owns the lifetime of the containing team component.

`cyclo task` does not start that component. It invokes an allowlisted AgentWS
queue tool in a one-shot instance of the same immutable team image. The tool has
no network, project, team, Pi, credential, or Docker authority; it receives only
the task/job queue mounts required by that operation and starts directly as the
mapped non-root identity. Task and explicit job creation mount a bounded,
link-resistant snapshot staged under Cyclo state, never the live project path.

The host must not be root. The base image is built for the invoking user's
UID/GID. The entrypoint starts with image root only to select that identity.
After dropping privilege it copies the immutable Pi settings template into the
team's writable Pi state and executes the runtime. Filesystem access to mounted
state is decided by the host rather than a Cyclo ownership or mode policy.

## State ownership

The state split is intentional:

| Owner | Durable state |
| --- | --- |
| Cyclo | `host.conf` selection, instance `run.json`, desired running/stopped intent, AgentWS queues, Pi state, generated project views, generated DComp definitions, Docker endpoint binding |
| DComp | Applied-system record, immutable Docker object identities, network and component reconciliation state, interrupted operation journal |
| Gateway | Credential/account store, refreshed OAuth sessions, model catalogue snapshot, usage ledger in its named Docker volume |
| Docker | Images, containers, networks, volumes, health and current published-port allocations |

DComp state lives below `STATE_ROOT/dcomp`, but its schema and lifecycle belong
to DComp. Cyclo accesses it only through DComp's machine API. Conversely, DComp
does not parse Cyclo instance records or project/team definitions.

Cyclo instance records contain domain intent and immutable image IDs. They do
not contain container IDs, network IDs, or a second lifecycle state machine.
They are created by `cyclo run` under
`STATE_ROOT/instances/INSTANCE/run.json`; team instances are not host
configuration entries.

## Operations

Mutating Cyclo commands serialize through one realm lock. The important
operations are:

- `run`: validate current project/team sources, ensure the provider system,
  validate models, build team images, persist running intent, and apply the
  complete system;
- `start`: change a persisted instance to running and apply;
- `stop`: change selected instances to stopped and apply;
- `shutdown`: remove every runtime container and transient network for the
  selected realm, verify their absence, and preserve instance intent and
  persistent volumes;
- `forget`: require stopped intent, apply to prove absence, then delete the
  instance and AgentWS state;
- `refresh`: re-read and build all running project/team replacements, validate
  them against the current Provider catalogue, publish the replacement cohort,
  apply it, and require the refreshed teams to become ready;
- `repair`: apply the current host configuration and persisted instance intent,
  running the required host Docker builds and resuming interrupted DComp work;
- `models`: apply the system and query the outer Provider catalogue; and
- `gateway login/logout/rename`: prepare only the fixed gateway/store boundary,
  atomically update the private credential store, and restart the gateway. They
  do not reconcile unrelated Provider or team components; logout is local
  removal rather than provider-side revocation.

`ps`, `inspect`, `logs`, generic component inspection/restart,
`providers status`, `gateway status`, `doctor`, and the fleet dashboard inspect
persisted state and DComp facts. Generic component restart controls an
already-applied component directly; it does not reconcile provider or instance
configuration. These commands do not create an alternative lifecycle model.

The outer Provider selected by `host.conf` is the only authoritative route. A
failed component is reported as not ready and the DComp system is
non-operational until the configuration or component is fixed.

## Security architecture

The trusted administrative domain is the host OS, Cyclo CLI, DComp executable
and state, Docker daemon, operator-approved configuration, image build inputs,
and every host account granted access to the default realm's state. Such
accounts can administer the same gateway accounts, Providers, teams, queues,
and lifecycle.
The primary hostile workload is arbitrary code inside a team container.

Cyclo enforces these boundaries before emitting DComp binds:

- team and project source trees must be canonical, distinct, and non-overlapping;
- no declared mount may overlap Cyclo state, installed Cyclo sources,
  `host.conf`, the DComp executable, the host Pi directory, `/proc`, `/sys`,
  `/dev`, `/run`, or a known Docker socket;
- initial launch rechecks bind-source device/inode identity after validation;
- every later apply revalidates persisted mount authority;
- team containers receive no Docker socket or gateway volume;
- provider links are private networks with only their two endpoints;
- the bundled OpenAI edge defaults to host loopback and requires an explicit
  `host.conf` setting for a wider bind; and
- credentials remain in the gateway volume.

Team Dockerfiles and provider Dockerfiles execute through the trusted Docker
daemon. Approving either source is a host-administration action; runtime
isolation cannot make an untrusted Docker build safe.

Normal teams have outbound network access, so an agent may exfiltrate data from
its declared readable mounts. `--offline` removes direct external routing, but
does not make the model Provider confidential: data sent for inference reaches
the selected provider chain and external model service. Additional policy,
quota, audit, or filtering components can be interposed when required.

The fleet and AgentWS dashboards are unauthenticated. Their default bind is
loopback. A non-loopback deployment must use a trusted network boundary or
authenticated reverse proxy.

Separate state roots prevent accidental resource adoption and name collisions;
they are not a security boundary against the trusted host, other accounts with
realm access, or the Docker administrator. Stronger tenant isolation requires
separate OS or VM domains.
