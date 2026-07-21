# Cyclo architecture

Cyclo keeps four lifecycles separate: Git-defined teams, project workspaces,
model composition, and credentials. The credential gateway is deliberately not
the provider orchestrator.

## Components

```text
                         project.cyclo
                 teams + named mounts + modes
                    /                    \
                   v                      v
       team repositories          mounted directories
       team + roles/*.md          projects + read-only inputs
                   \                      /
                    +--------+-----------+
                             v
                  team runtime containers
             one isolated instance per team
                agents + filesystem job loop
                      |
          scoped per-instance bearer
                      | TCP on team network
                      v
             provider runtime container
      catalogue + composition routes + policy
          | UDS                         | private Docker network
          v                             v
 provider component containers   credential gateway container
 network=none                    credentials + concrete proxy + usage
                                        |
                                        v
                               concrete model services
```

## Ownership contract

Cyclo assigns every internal control-state transition to one component. Project
and writable-team files remain ordinary shared filesystem state. A consumer
uses the producer's protocol; it does not inspect the producer's internals or
infer failure causes from an unrelated health signal.

| Component | Owns | Must not own |
|---|---|---|
| Host `cyclo` controller | Configuration parsing, mount and network validation, explicit Docker lifecycle, persisted instance state, capability publication and revocation, and host-side operational checks | Agent job settlement, model routing, or physical credentials |
| Cyclo team PID 1 | Queue recovery under the exclusive runtime lock, interpretation of runner exit, and supervision of the AgentWS runner and read-only viewer | Provider-stack health policy, model-failure classification, or job settlement |
| AgentWS | Durable task/job state, `claim -> execute -> settle`, bounded engine retries, terminal planner notification, and cooperative engine-process cleanup | Provider/gateway health policy, endpoints, identities, boot identities, or outer container-lifecycle decisions |
| Provider runtime | Merged catalogue, team model authorization, virtual routes, provider registration, and short-lived request contexts | Physical provider credentials, team queues, or container lifecycle |
| Provider component | One virtual provider transformation declared by `host.conf` | Team identity, real credentials, sibling components, or Docker/network control |
| Credential gateway | API keys, subscription sessions, concrete account/model routes, upstream calls, and concrete usage accounting | `host.conf`, virtual composition, provider-component lifecycle, or team queues |
| Dashboards | Bounded read-only projections of instance, queue, runtime, and usage state | Lifecycle or queue mutation |

The host controller is Python. Python types named `RuntimeContainer`,
`ProviderService`, and `ProviderRuntime` are host control-plane adapters for
Docker resources; they are not code executing inside the provider-runtime
container. The model engines, provider runtime, and credential gateway execute
inside packaged images; Node.js is a maintainer requirement, not a normal host
runtime requirement. A multi-team project creates one independent team runtime
per team. Only the gateway mounts
`cyclo-gateway-store`; no team or provider component receives that volume, a
real provider key, or a subscription session.

## Protocol contract

| Boundary | Transport | Contract |
|---|---|---|
| Host controller -> Docker | Docker CLI on the host | Create, inspect, attach, stop, and remove only explicitly owned resources; no container receives the Docker socket |
| Host controller -> provider runtime | Atomic replacement of bind-mounted registries plus authenticated HTTP/1.1 over the mode-`0600` control Unix socket | The control capability permits only `GET /providers` and the two explicit reload/refresh POSTs; it cannot invoke inference |
| Host controller -> credential gateway | Atomic replacement of the hash-only client registry, distinct catalogue-only and usage-only loopback capabilities, and explicit one-shot login containers | Publish concrete authorization, provision accounts, and observe usage without an unrestricted gateway principal |
| Cyclo team PID 1 -> AgentWS | Local subprocess, inherited runtime-lock file descriptor, and exit-status protocol | Recover orphaned jobs before startup, supervise the runner/viewer, and escalate an unsafe runner exit to container teardown |
| AgentWS worker -> AgentWS queue | Filesystem state through bundled `bin/` commands and locks | Claim one role-compatible job, execute one engine attempt, and durably settle that claim |
| AgentWS worker -> model engine | One fenced process group using standard streams, or Pi's line-delimited RPC mode | Execute an attempt and reduce engine completion to durable queue state plus a worker exit status |
| Team model engine -> provider runtime | Bearer-authenticated HTTP over the team's private Docker network | List only the team's allowed models and invoke an allowed route |
| Provider runtime -> credential gateway | HTTP over the separate runtime/gateway private network | Forward the original opaque team bearer; the gateway independently reauthorizes its concrete scope, substitutes the real credential, and records usage |
| Provider runtime <-> provider component | HTTP/1.1 over two prefix-specific Unix sockets | Invoke one virtual route and allow only its declared upstream inputs, using distinct capabilities |
| Dashboard -> system state | Read-only filesystem, Docker inspection, and GET requests | Observe bounded snapshots; never execute queue content or change state |

AgentWS settlement is intentionally cause-blind. If the engine exits while it
still owns a job, the attempt is charged and the job is either released for a
bounded retry or terminally failed after a deterministic planner notification.
An operator SIGINT/SIGTERM restores the previous attempt count and releases the
job. Unsafe engine cleanup or an unprovable queue transition is a fatal worker
exit handled by the team PID 1 and Docker process fence. For a queued Pi RPC
worker, rejection of the initial command is an engine failure and
`agent_settled` ends an accepted attempt. The durable job state then determines
whether the worker completed or follows the ordinary bounded-retry path. Pi's
long-lived RPC process is an implementation detail, not an attempt boundary.

Provider-stack health belongs to Cyclo's control and observation surfaces.
`cyclo doctor`, `cyclo ps`, and the fleet dashboard distinguish team-container
state from provider-runtime and gateway state. They do not mutate AgentWS queue
state or retroactively reclassify an engine result. If a future engine can
report a temporary dependency failure reliably, that must be an explicit typed
engine outcome; Cyclo must not infer it by comparing health probes or process
identities around an engine exit.

## Project definitions

The normal run interface is a strict, line-oriented `project.cyclo` file:

```text
name core-et-uart
description Design and verify a UART IP.
team ../teams/jon-rtl ro
team ../teams/rtl-auditor ro
mount source ../sources/core-et rw
mount specifications ../references/specifications ro
```

`name` and `description` occur exactly once; at least one `team` and one
`mount` are required. Team and mount lines carry an explicit `ro` or `rw`
mode. A `rw` mount is a writable project; a `ro` mount is a read-only supporting
input. Relative paths resolve from the definition's directory, not the caller's
working directory. Resolved team and mount trees must be unique and mutually
non-overlapping, and the complete collection is checked against Cyclo state,
trusted runtime/configuration paths, pseudo-filesystems, host Pi state, and
Docker sockets before any team starts. Unknown directives fail closed; `mcp`
is reserved but rejected until Cyclo implements MCP attachment.

`cyclo run project.cyclo` starts one independent instance per team. Each
container sees only its selected definition at `/team`, with the line's access
mode. Writable mounts appear at `/workspace/<name>` and read-only inputs appear
at `/readonly/<name>`. Both parents are generated read-only namespaces, so
undeclared top-level paths cannot be created. Cyclo writes a separate,
host-path-free `/agentws/PROJECT.md` into each runtime and includes it in every
agent's initial prompt; it records the project name, description, definition
digest, logical mount paths, and modes. This project context is independent of
and remains authoritative alongside a team's optional `AGENTS.md`.

Every team and mount is preflighted before the first start. If a later start
fails, Cyclo stops and revokes the instances already started by that invocation.
Each instance still owns its own network, model capability, queue, Pi state, and
viewer. The compatibility form `cyclo run TEAM PROJECT` retains the former
single-team writable `/workspace` binding and its `--team-write` flag, but new
composable experiments use `project.cyclo`. The exact grammar is documented in
[`project-format.md`](project-format.md).

## Host provider configuration

Optional installation-wide composition is a small line-oriented file,
`/etc/cyclo/host.conf`:

```text
# provider PREFIX PATH INPUT_MODEL... [KEY=VALUE ...]
provider fusion ./providers/fusion codex-work/MODEL_ID mode=balanced
```

`PREFIX` is the component's output namespace. `PATH` is a local build context
containing a `Dockerfile`; relative paths resolve from the directory containing
`host.conf`, never from the caller's working directory. Inputs are exact
`provider/model` names and must precede component-owned `key=value` arguments.
Lines are dependency order, so an input may refer to a concrete gateway account
or an output from an earlier line. Forward references, cycles, unknown inputs,
duplicate prefixes, and collisions with concrete accounts fail closed.

The provider runtime receives exactly the canonical `host.conf` file as a
read-only bind at `/etc/cyclo/host.conf`; no sibling host files are exposed.
The runtime parses it once at startup. Every edit—including an in-place write,
creation or removal, symlink retarget, or inode replacement—requires an explicit
runtime restart before it takes effect. Run `cyclo runtime restart` for that
operation; configuration changes never require an image rebuild.

A missing or empty file is a valid configuration: the provider runtime exposes
the gateway's concrete catalogue unchanged. At startup the runtime combines its
parsed configuration, concrete gateway catalogue, expected provider state, and
validated persisted registrations into one immutable in-memory snapshot.
Normal catalogue and inference requests use that snapshot directly: they do not
reread files, refetch the whole catalogue, or health-probe every component.

A successful component registration validates the startup configuration and
expected launch state, probes that component, sanitizes and durably records its
registration for restart recovery, and atomically publishes a replacement
snapshot. On runtime restart, a persisted registration is
recovered only after it still matches the new configuration and expected state
and its component passes the health probe. The active route table itself exists
only in process memory. Removing a `host.conf` line therefore hides the route
after the required runtime restart without silently deleting its container.
Changing a provider path or argument also changes its configuration identity,
so the old component cannot be recovered under the edited definition; restart
that provider explicitly to publish a matching generation.

The provider runtime's expected-provider registry, sanitized registration
recovery records, component capabilities, socket directories, and client
records live in a writable bind below Cyclo's host state root. They are separate
from both `host.conf` and the gateway credential volume.

Expected-provider and client registries are dynamic security authority. The
controller normally replaces them atomically and requests an authenticated
reload for a synchronous `204` acknowledgement. The runtime also compares only
their file identities every 500 ms and runs that same reload transaction after
a replacement. This is a crash backstop: if the controller is killed after the
durable revocation but before its control call, cached authority is still
revoked within a bounded interval. A changed malformed registry revokes all
dynamic clients and component routes until a valid replacement appears.
`host.conf` is deliberately excluded from this watcher and remains
restart-only.

## Explicit lifecycle

Shared services and provider components are operated explicitly:

```sh
cyclo gateway restart [--build]

cyclo runtime start [--build]
cyclo runtime restart [--build]
cyclo runtime stop
cyclo runtime status

cyclo provider build PREFIX          # or: cyclo provider build --all
cyclo provider start PREFIX          # or: cyclo provider start --all
cyclo provider restart PREFIX        # or --all; add --build when wanted
cyclo provider stop PREFIX           # or: cyclo provider stop --all
cyclo provider status PREFIX         # or: cyclo provider status --all
```

The initial runtime start normally uses `cyclo runtime start --build`; later
starts require the already-built image. `provider start` requires a current
image and never builds one. `provider restart --build` is the explicit combined
operation. No command stops a configured-but-omitted provider as a side effect.
Runtime start validates, but never repairs, the credential boundary: the
gateway must be current and attached only to its owned private network. A
legacy gateway still attached to a team network is rejected with an explicit
`cyclo gateway restart` instruction.

`cyclo models` asks the running provider runtime to refresh its concrete
gateway catalogue, then queries it. `cyclo run` requires that runtime to be
running. Neither command starts, rebuilds, replaces, or stops the gateway,
provider runtime, or provider components. A team run may still build its own
per-team runtime image when needed.

`cyclo gateway restart` replaces only the credential gateway and preserves its
credential volume. `cyclo runtime restart` replaces only the provider runtime
and preserves its host-bound recovery state; use it after every `host.conf`
update. The restarted runtime revalidates and probes persisted registrations
before admitting them to its new in-memory snapshot. Image rebuilds are tied to
changed program code or an explicit `--build`, not to `host.conf` contents.
Gateway login and restart refresh the concrete catalogue of a running runtime
through a separate authenticated control operation. That operation never
reloads `host.conf`.

An upgrade from the former unrestricted gateway-token contract is deliberately
ordered: run `cyclo gateway restart --build`, then
`cyclo runtime restart --build`. The gateway restart creates fresh catalogue
and usage capabilities, verifies the replacement, and only then removes the
exact legacy unrestricted-token files. The runtime restart mounts the new
catalogue-only capability. Ordinary later gateway login or restart can refresh
an already-current runtime without replacing it.

`cyclo provider restart PREFIX` first publishes and acknowledges removal of
that prefix's expected state and upstream capability, then stops the old
component. Only afterward does it publish replacement authority and launch the
new process, with fresh ingress and upstream tokens. This revocation boundary
also removes the old recovery record, so
a same-generation replacement cannot be mistaken for an idempotent
registration through Unix-socket inode reuse.

## Model request path

A team calls `POST /p/PREFIX/<native-path>` on the provider runtime over its
private team network. The runtime validates the team's exact public
provider/model scope. Authentication binds the team's capability hash to the
runtime's local address on that specific team network; possessing another
team's bearer is therefore insufficient to replay it from the caller's own
network.

The shared listener admits at most 32 TCP connections on any one team-facing
runtime interface, 256 TCP connections globally, eight active requests per
project/provider principal, and 24 untrusted root requests globally. Nested
calls use a separate pool charged to the originating project: 16 per origin and
32 globally. At most 12 root bodies and 24 nested bodies are retained globally.
Bodies remain capped at 16 MiB and must finish within 30 seconds; active
admission is held through the complete upstream response, while body admission
is released after upstream response headers. Host control uses its
separate Unix listener, so a team filling network or workload budgets cannot
block revocation. Each provider Unix listener is separately capped at 64, and
requests on it use a 200/s prefix-local token bucket. Team-facing TCP requests
use a 500/s token bucket per private-network interface. Authenticated
registration attempts are additionally serialized and rate-limited per prefix.

For a concrete route, the runtime forwards the same opaque team bearer to the
gateway over the runtime/gateway private network. The gateway resolves that
bearer against its transitive concrete scope, swaps it for the selected real
credential, calls the upstream service, and attributes usage to the original
team/project generation.

For a virtual route, the runtime calls the component over its private Unix
socket with a route-local ingress token and an opaque
`X-Cyclo-Request-Context`. The component calls declared inputs back through the
runtime's Unix socket using a separate upstream token plus that context. The
runtime recovers the original team bearer from in-memory request state and
uses it for any concrete gateway call. Consequently:

- the gateway retains physical usage attribution to the original team;
- a component never sees the team bearer or a real credential;
- a component can call only the inputs declared on its own `host.conf` line;
- nested composition does not create a provider-to-provider transport.

The provider runtime mounts the gateway's catalogue-only capability to read the
concrete catalogue. The usage capability remains available only to the host
controller and gateway. Neither capability is accepted for inference; concrete
requests always use the original scoped client/team bearer.

## Networks and sockets

Every team instance has a private Docker network. Its team runtime and the
shared provider runtime join that network; the credential gateway does not.
The provider runtime and gateway share a different private network. The host
controller publishes network ports only on loopback; provider-runtime control
uses a separate mode-`0600` host Unix socket. Cyclo reads the runtime's address
on each attached team network when publishing client capabilities. Missing
attachment produces an unusable binding rather than a token-only fallback.

Provider components run with `--network none`, a read-only root, reduced
privileges, bounded resources, and no Docker socket. HTTP/1.1 uses two
host-managed Unix-socket directions:

- every component can reach one prefix-specific provider-runtime socket through
  its own read-only directory mount;
- the provider runtime can reach each component's socket through that
  component's separate read-only directory mount.

Components cannot mount or scan one another's socket directories, nor can they
mount the sibling host-control socket. One provider exhausting its own UDS
connection cap therefore cannot occupy another provider's or the controller's
listener. The filesystem topology provides reachability; bearer capabilities
provide authorization. HTTP retains streaming, backpressure, cancellation,
and language-neutral framing. The normative contract is
[provider protocol v1](provider-protocol.md).

Team containers are launched without `CAP_NET_RAW`. This makes the per-team
runtime-interface capability binding hold even for a custom image or a Cyclo
invocation running as root: the team cannot forge raw traffic addressed to the
runtime's interface on a different private network.

Normal instance mode may also have direct outbound networking. `--offline` makes
the team network internal while preserving its route to the provider runtime.
It does not make readable project data confidential from an allowed model: an
agent can still include that data in a model request.

## Teams, projects, and durable work

A team is an ordinary Git repository containing a `team` roster, role prompts,
and optionally `AGENTS.md`. It is a declarative input, not a Component or
Provider service. The roster binds each agent to a role, engine, and exact
provider/model. A generation combines the repository commit with a digest of
the live definition, so runs remain attributable with uncommitted edits. The
project definition has its own semantic digest over its name, description,
ordered teams, ordered named mounts, resolved paths, and modes.

The accepted component-system cutover also permits an optional team
`Dockerfile` that derives from a Cyclo-compatible base through
`ARG CYCLO_TEAM_BASE` and `FROM ${CYCLO_TEAM_BASE}`. This is a build recipe for
extra tools, not a copy of AgentWS, Pi, or provider code. Team generation and
image generation remain separate: the latter covers the exact base image ID
and effective Docker build context. A successful candidate is ABI-validated
before its tag is promoted, and instances run and record its exact image ID.
Ordinary runs never execute a repository Dockerfile implicitly. This derived
image path is an accepted design but is not wired into the 0.2.0 CLI; see
[Team repositories](team-repositories.md).

In normal `project.cyclo` operation, every team and mount has an explicit mode.
A `rw` team may self-modify; a `rw` mount is a project that may be changed by
every team in that project. A `ro` mount is supporting input, never a read-only
project. Concurrent writers therefore have ordinary filesystem races,
and separate Git worktrees remain the isolation mechanism when required. The
legacy `cyclo run TEAM PROJECT` form keeps a read-only team and writable project,
with `--team-write` available for deliberate team self-modification.

Mount validation is both per-definition and cross-instance. A new source may
exactly reuse an existing source of the same kind—for example, two teams sharing
one declared project checkout—but it may not be a parent or child of a running
instance's source, or reuse a team source as a project source. After parsing,
Cyclo records the selected device and inode in the invocation's run bindings
and rechecks them immediately before Docker creates the container. Each launch
also has an independent identity;
multi-team rollback removes only the container launched by that invocation.

Tasks, jobs, comments, transcripts, and results live below the selected Cyclo
state root and survive container replacement. The runtime sees a materialized,
read-only copy of the bundled queue implementation and writable per-instance
queue and Pi state. Stopping an instance removes its container and private
network, revokes its model capability, and preserves its queue history.

## Persistent state

By default, controller and provider-runtime state is below
`$XDG_STATE_HOME/cyclo` (or `~/.local/state/cyclo`):

```text
cyclo/
  gateway/                       # client records plus catalog/usage capabilities
  provider-runtime/              # registry, tokens, registration recovery, sockets
  instances/<instance>/
    run.json
    runtime/                     # bundled filesystem loop copy
    workspace/                   # inert namespace for writable projects
    readonly/                    # inert namespace for read-only inputs
    agentws-state/               # tasks, jobs, comments, results, transcripts
    pi/agent/                    # projected model config and scoped runtime bearer
```

Physical credentials, subscription sessions, and the append-only concrete
usage ledger are not in that tree. They live in the Docker-managed
`cyclo-gateway-store` volume. `cyclo gateway restart`, team stop, and ordinary
state cleanup do not delete it. `cyclo gateway destroy-store` is the separate,
explicit destructive operation.

The gateway's catalogue and usage capabilities live in separate mode-`0600`
canonical host files. On explicit gateway restart, Cyclo atomically projects
each into its own container-readable file beneath a mode-`0700` host directory
and bind-mounts only those projected files into the gateway, read-only. The
provider runtime instead receives a read-only bind of the canonical catalogue
file; it never receives the usage capability. Token bytes never appear in
Docker arguments or environment values, and neither capability can invoke
inference.

The provider runtime has a different control capability on its private
mode-`0600` Unix socket. It permits `GET /providers`,
`POST /_cyclo/v1/control/reload`, and
`POST /_cyclo/v1/control/refresh-catalog` only. Workload paths are rejected.

## Observation and host trust

The per-team AgentWS viewer and fleet dashboard are read-only and bind to
loopback by default. They have no application authentication in 0.2.0; a
non-loopback bind should be protected by the host network policy. Dashboard
queue reads are bounded and never execute queue content.

Cyclo assumes the host kernel, Docker daemon, Cyclo installation, provider
runtime, and credential-gateway images are trusted. Docker administrators can
inspect containers and volumes. Installed provider source is privileged build
input and must be reviewed before `cyclo provider build`; runtime isolation
does not make a malicious Dockerfile safe to build.

Team and project trees are untrusted agent-controlled data. Cyclo does not mount
the Docker socket, host home, gateway volume, another instance's queue, or an
undeclared project tree into a team container. Teams intentionally share only
the named mounts in their common `project.cyclo`. Writable Git trees may contain
hostile hooks or configuration and should be treated accordingly by later
host-side commands.

Selecting a team Dockerfile for a build changes its status from passive data to
privileged build input: Docker executes its instructions through the host
daemon. That operation requires explicit operator intent and review, receives
no Cyclo credentials or project mounts, and is never triggered by a normal run
or a writable team modifying itself. Container isolation cannot make an unsafe
Dockerfile safe to build.
