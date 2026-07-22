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

The host `cyclo` command constructs this graph and manages Docker lifecycle. It
is not a service in the data path.

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
Cyclo state roots and host configurations provide independent installations
when several are operated in one trusted administrative domain.

The canonical state-root path is the installation identity. Cyclo hashes that
path to a stable 12-hex-character ID and scopes all owned Docker resources with
it: gateway/provider containers and images, the credential volume, team
containers and networks, the default team image, and ownership labels. The
state root also contains that installation's queues and Unix sockets. Therefore
two installations can reuse logical project, team, provider, and instance names
without sharing mutable Docker names. A user-selected custom image is explicit
shared authority and is not renamed.

The host configuration is selected independently because it describes the
installation's provider graph, not its identity. Operationally, a deployment
must consistently pair one state root with one host configuration. Status and
lifecycle commands discover resources from that pair and refuse resources
labelled for a different installation. This is namespace separation inside one
trusted Docker host, not a claim of protection from that host's administrator;
mutually distrustful administrators still require separate OS/VM boundaries.

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
runtime are consumers of the outer Provider interface, not provider-stack
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

The declaration contains no endpoint addresses. The host assembly binds the
named `upstream` requirement to a producer. Cyclo mounts the producer's socket
directory read-only at `/run/cyclo/requirements/upstream`; the component owns
`/run/cyclo/component.sock` in its output directory.

Component containers use their image's immutable entrypoint and healthcheck,
run with a read-only root, private IPC/cgroup namespace, dropped capabilities,
bounded PIDs/file descriptors, a small temporary filesystem, and the exact
socket mounts implied by their declaration. Intermediate components use
`--network none`. No component receives the Docker socket.

Cyclo builds under a temporary candidate tag, validates the completed image,
and only then moves the component's official tag to it. Runtime status checks
that the container uses that exact image ID, plus container ownership, launch
configuration, mounts, isolation, engine health, and the component's `Health`
RPC. “Container running” alone is not readiness.

## Provider stack

`/etc/cyclo/host.conf` is an ordered assembly:

```text
# provider INSTANCE SOURCE [context=PATH] REQUIREMENT=TARGET ... [-- ARGUMENT ...]
provider trace ./providers/passthrough upstream=gateway -- label=first
provider outer ./providers/passthrough upstream=trace
```

`INSTANCE` is the host-local component name. `SOURCE` contains `Dockerfile` and
`component.conf`. Requirement bindings name `gateway` or an earlier component;
forward references and missing or mismatched interfaces fail before Docker is
called. Arguments after `--` are passed as distinct OCI arguments. Relative
paths resolve beside `host.conf`.

The fixed gateway is always the root. If `host.conf` is absent or empty, the
gateway socket is the outer provider endpoint. Otherwise the final declared
component socket is the endpoint mounted into team containers. Editing the
file changes the expected assembly; apply it explicitly with:

```sh
cyclo providers restart --build   # rebuild only when component source changed
```

Normal lifecycle commands never infer or repair a different assembly:

```sh
cyclo gateway build|start|restart|stop|status
cyclo providers check|build|start|restart|stop|status
```

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
the payload. All other JSON Pi options pass without a Cyclo allowlist.

The normative details are in [Provider protocol v1](provider-protocol.md).

## Gateway boundary

The gateway is an independent root component. Its Docker volume contains
credentials, OAuth sessions, and the usage ledger. No team or intermediate
component mounts that volume. The public model catalogue exposes account/model
names and safe capabilities, never native headers, base URLs, or credentials.

`cyclo gateway login` updates the private store. The long-running gateway reads
credential values dynamically, while its model catalogue is a startup snapshot;
restart it after login to publish catalogue changes. OAuth refreshes use a
kernel lock and atomic file replacement.

Incoming ConnectRPC headers are not forwarded to native services. The gateway
chooses the native model from its catalogue, resolves the matching credential,
overrides credential/transport controls, and records observed Pi usage. Usage
observation does not mutate or reorder the response payload stream.

## Projects and teams

`project.cyclo` is the complete authority for a run:

```text
name core-et-uart
description Design and verify a UART IP.
team ../teams/jon-rtl ro
team ../teams/rtl-auditor ro
mount source ../sources/core-et rw
mount specifications ../references/specifications ro
```

Cyclo starts one independent instance per team. A `rw` mount is a project at
`/workspace/NAME`; a `ro` mount is supporting input at `/readonly/NAME`. Team
mode controls whether `/team` itself is writable. Relative paths resolve beside
the definition. All selected trees must be real, non-overlapping directories.

Before the first container starts, Cyclo validates every team, requested model,
mount, provider-stack readiness, and bind-source identity. A partial multi-team
startup rolls back only containers created by that invocation. Queue history
remains under the state root.

A team repository contains only its data:

```text
team
roles/*.md
AGENTS.md          # optional
```

The roster line format is:

```text
AGENT ROLE ENGINE PROVIDER/MODEL
```

Cyclo supplies the common AgentWS runtime and Pi extension in the team image.
It generates `/agentws/PROJECT.md` with logical mount paths and requires agents
to read it. The host paths never need to be embedded in team prompts.

## Team isolation and state

Team containers mount:

- the selected team at `/team` with its declared mode;
- writable projects below `/workspace`;
- read-only inputs below `/readonly`;
- a read-only materialized AgentWS runtime;
- writable queue and Pi state owned by that instance; and
- the final Provider socket directory read-only.

The provider socket is authority to use the configured model catalogue; no API
key or subscription session enters the team. `--offline` removes ordinary
network egress while leaving the Unix provider socket available.

Persistent state defaults to `$XDG_STATE_HOME/cyclo` or
`~/.local/state/cyclo`:

```text
instances/INSTANCE/
  agentws-state/          tasks, jobs, agents, comments, and results
  pi/                     Pi settings and runtime metadata
  runtime/                generated read-only AgentWS runtime
  run.json                persisted instance metadata
sockets/gateway/          root component socket
sockets/COMPONENT/        intermediate component sockets
```

Physical credentials and the usage ledger live instead in a separately owned
Docker volume. `cyclo gateway status` prints its installation-specific name.
Ordinary stop/restart operations do not delete it; `cyclo gateway
destroy-store --confirm VOLUME` is the explicit destructive operation.

New team resources use `cyclo-SYSTEM-team-INSTANCE` containers,
`cyclo-SYSTEM-team-INSTANCE-net` networks, and the
`cyclo-SYSTEM-team:VERSION` default image. `SYSTEM` is derived from the state
root. Labels record the same system, resource kind, and logical instance, so a
name alone is never sufficient authority for lifecycle operations.

## Failure model

- AgentWS settles engine attempts from durable queue state and bounded retry
  rules. Provider health does not retroactively reinterpret an agent exit.
- A component is ready only when its exact current container and dependencies
  are ready and its health RPC succeeds.
- Unknown models fail before native dispatch.
- Connect failures remain transport failures; provider failures already emitted
  as Pi events remain Pi events.
- Configuration is declarative and restart-applied. Cyclo does not silently
  rebuild images or mutate configuration to make a failed health check pass.

`cyclo doctor` checks the installed AgentWS/component ABI, persisted state,
Docker, host assembly, gateway, every intermediate component, and the outer
catalogue without changing the system.
