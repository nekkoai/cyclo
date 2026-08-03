# Cyclo 0.2 system engineering specification

## 1. Status and scope

This document specifies the Cyclo 0.2 host architecture and its contract with
DComp, Docker, gateway/provider components, team repositories, and project
definitions.

The specification covers:

- authority and ownership boundaries;
- persistent state;
- component compilation and links;
- image construction and identity;
- lifecycle operations and crash behavior;
- Provider transport;
- team execution and mount policy; and
- operator-visible health.

Cyclo 0.2 is a fresh-install boundary. A conforming implementation MUST reject
state whose schema belongs to the pre-DComp lifecycle rather than silently
adopting it.

The terms MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are normative.

## 2. Architectural principles

### 2.1 Single responsibility

Cyclo MUST own:

- domain configuration parsing;
- instance intent and AgentWS state;
- Docker image construction and validation;
- generation of the DComp system; and
- the public operator interface.

DComp MUST own:

- long-lived component container lifecycle;
- component link and base networks;
- declared persistent-volume lifecycle;
- immutable Docker object identity for applied systems; and
- interrupted lifecycle-operation recovery.

Docker remains the physical store and execution engine for images, containers,
networks, volumes, health, and published ports.

The gateway MUST own physical credentials, OAuth refresh, native provider
calls, the root model catalogue, and usage history.

AgentWS MUST own task, job, comment, result, retry, claim, and planner
coordination semantics.

No subsystem may maintain a second authoritative copy of another subsystem's
lifecycle state.

### 2.2 No host service

Cyclo MUST be a host CLI, not a daemon. DComp MUST be invoked as a host CLI, not
installed as a service registry, proxy, sidecar, or data-plane daemon.

After a successful apply, component traffic MUST travel directly between
component containers. Cyclo and DComp MUST NOT be in the inference data path.

### 2.3 One global composition

One canonical Cyclo state root MUST correspond to exactly one installation-wide
DComp system. Its logical name is:

```text
cyclo-<installation-id>
```

where `installation-id` is a stable digest of the canonical state-root path.

The complete desired system MUST contain:

1. the gateway;
2. every Provider component declared by the selected `host.conf`; and
3. every persisted team instance with `running` intent.

Cyclo MUST compile and apply this complete set as one system. It MUST NOT start
each Provider or team through an independent lifecycle implementation.

## 3. Trust model

### 3.1 Trusted administrative domain

Cyclo assumes these are trusted together:

- the host OS and account running Cyclo;
- the Cyclo package and state root;
- the DComp executable and its state;
- the selected local Docker daemon;
- operator-approved configuration, component sources, Dockerfiles, and images;
  and
- gateway implementation and storage.

An agent process and arbitrary code executed inside a team container MUST be
treated as hostile.

Provider components are trusted to observe and transform traffic on their
declared links. They are not trusted with physical gateway credentials unless
they are the gateway itself.

### 3.2 Out-of-scope authority

Cyclo does not defend against a compromised host account, root user, Docker
administrator, DComp binary, or operator-approved Dockerfile. Stronger
administrative isolation requires a separate OS or VM.

Separate Cyclo state roots prevent accidental resource collision and adoption.
They are not a kernel or Docker-administrator security boundary.

## 4. Installation selection

### 4.1 State root

Without `--state-root` or `CYCLO_STATE_ROOT`, Cyclo MUST use:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/cyclo
```

An explicitly selected state root MUST be made canonical before use.

### 4.2 Host configuration scope

The implicit state root MUST read:

```text
/etc/cyclo/host.conf
```

An explicit state root MUST read:

```text
STATE_ROOT/host.conf
```

Cyclo MUST record whether an installation uses system or local host
configuration. A later invocation requesting the other scope for the same state
root MUST fail.

A missing host configuration is equivalent to an empty file.

### 4.3 Docker endpoint

On the first operation requiring a Docker endpoint, Cyclo MUST resolve Docker's
selected context to a canonical local Unix socket and persist that endpoint.

All later Docker and DComp mutations for the installation MUST use the persisted
endpoint. If the currently selected endpoint differs, Cyclo MUST fail.

Cyclo MUST reject remote Docker schemes. It MUST NOT mount the Docker socket
into a component.

### 4.4 DComp executable

Cyclo MUST select DComp from:

1. `CYCLO_DCOMP`, when nonempty; or
2. the first `dcomp` on `PATH`.

The resolved executable path MUST be absolute. An empty override or missing
executable MUST fail with an actionable error.

Before stateful use, Cyclo MUST execute:

```text
dcomp version --json
```

and require machine `api_version` 1. Cyclo MUST NOT parse human-readable DComp
output.

All stateful calls MUST include:

```text
--state-root STATE_ROOT/dcomp
```

Cyclo MUST resolve a DComp-owned volume only through:

```text
dcomp volume --json SYSTEM COMPONENT LOGICAL_NAME
```

Machine API 1 MUST return exactly `api_version`, `system`, `component`,
`logical_name`, and `name`. Cyclo MUST require API version 1, require all three
logical identifiers to match the request, and treat the returned nonempty
`name` as opaque. A nonzero exit or malformed response MUST fail the operation.
Cyclo MUST NOT reproduce DComp's physical resource-naming rules.

When the installation is bound to Docker, Cyclo MUST pass that endpoint through
`DOCKER_HOST` and remove `DOCKER_CONTEXT` from the DComp environment.

## 5. Configuration formats

### 5.1 Provider descriptor

Every Provider source MUST contain `component.dcomp`:

```text
docker IMAGE
input PROTOBUF_SERVICE LOCAL_NAME
output PROTOBUF_SERVICE LOCAL_NAME
```

The descriptor is UTF-8, line-oriented, at most 1 MiB, and supports blank lines
and `#` comments. It MUST contain exactly one `docker` directive.

Endpoint names MUST use DComp lower-case component-name syntax. Service names
MUST be fully qualified protobuf service identities.

Cyclo MUST require exactly one output whose service is
`cyclo.provider.v1.Provider`. Other inputs and outputs MAY be declared.

### 5.2 Host provider configuration

Each non-comment `host.conf` line has this grammar:

```text
provider NAME SOURCE [context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARGUMENT ...]
```

Cyclo MUST:

- reserve the name `gateway`;
- resolve relative `SOURCE` paths beside `host.conf`;
- reject `~` expansion and whitespace-containing component paths;
- require `SOURCE` to be a canonical directory;
- resolve `context=PATH` relative to `SOURCE` when it is not absolute;
- require `SOURCE` to be inside the selected build context;
- bind every declared input exactly once;
- reject unknown inputs, outputs, components, and nominal service mismatches;
  and
- treat tokens after `--` as a literal argument vector replacing the image
  command arguments.

Cyclo MUST collect all declared outputs before resolving bindings. Forward
references, fan-out, and cycles are therefore valid. Links are address
bindings, not startup dependencies.

The outer Provider MUST be the final declared Provider's unique Provider
output. If none is declared, it MUST be `gateway.provider`.

### 5.3 Team repository

A team repository MUST be a Git repository root and contain:

```text
Dockerfile
team
roles/*.md
```

`default.team` MAY be accepted in place of `team` for compatibility.

Each roster record MUST contain:

```text
AGENT ROLE ENGINE PROVIDER/MODEL
```

Agent names MUST be unique, every role MUST have a corresponding role file, and
at least one agent MUST have role `planner`. Supported engines are `pi` and
`pi-interactive`.

Optional `AGENTS.md` supplies team-wide policy. The required Dockerfile MUST
declare `ARG CYCLO_TEAM_BASE` before its final base selection and MUST use that
value as its final stage's base. The minimal valid Dockerfile is:

```dockerfile
ARG CYCLO_TEAM_BASE
FROM ${CYCLO_TEAM_BASE}
```

Cyclo MUST read team-definition files without following symlinks and MUST bound
their size.

### 5.4 Project definition

A project definition has this grammar:

```text
name NAME
description TEXT
context <<MARKER
LITERAL TEXT
MARKER
team PATH ro|rw
mount NAME PATH ro|rw
```

`name`, `description`, at least one team, and at least one mount are required.
`context` is optional and unique.

Relative paths MUST resolve beside the project file. Project files and source
paths MUST be regular/canonical and MUST NOT use shell expansion or quoting.
Unknown directives MUST fail closed.

`rw` mounts map to `/workspace/NAME`. `ro` mounts map to `/readonly/NAME`.
Teams map to `/team` with their declared mode.

## 6. Image construction

### 6.1 Ownership

Cyclo owns image builds. DComp receives only image references already resolvable
to local immutable IDs and MUST NOT be asked to infer Dockerfiles or build
contexts.

### 6.2 Stable tags and native Docker builds

Cyclo MUST use stable, installation-scoped tags for built images:

```text
cyclo-INSTALLATION-gateway:VERSION
cyclo-INSTALLATION-provider-NAME:VERSION
cyclo-INSTALLATION-team:VERSION
cyclo-INSTALLATION-team-NAME-PATH_IDENTITY:VERSION
```

Whenever an operation needs an image built from source, Cyclo MUST invoke
`docker build` with the real Dockerfile and context, the stable tag, and an
`--iidfile`. Docker alone owns context selection, `.dockerignore` semantics,
and layer-cache reuse.

Cyclo MUST NOT hash source trees to decide whether to invoke Docker, emulate
Docker's ignore rules, or persist a source-digest cache or image-build history.
It MAY retain a completed build result in memory only for the duration of one
CLI operation.

Cyclo MUST capture Docker output, report a bounded diagnostic on failure,
inspect the completed stable tag, and require the inspected ID to equal the ID
written to the `--iidfile`. After validating the image contract, Cyclo MUST
pass only the immutable `sha256:` ID to DComp or persisted instance state.

### 6.3 Gateway image

Cyclo MUST build the gateway from its source context, which includes the
Component and Provider protocol packages, under the stable gateway tag.

`cyclo gateway build` MUST invoke the gateway build, apply the complete system,
and restart the gateway. Other required images follow their ordinary build
paths.

### 6.4 Provider image

When `SOURCE/Dockerfile` exists, Cyclo MUST invoke Docker with the declared
context and the Provider's stable tag. When it does not exist, Cyclo MUST
inspect and require the descriptor's prebuilt image.

Every Provider image MUST define an OCI health check.

### 6.5 Team image

The common team image MUST contain:

- the complete AgentWS runtime and generic protocol;
- the Cyclo team supervisor;
- Pi and the Provider adapter;
- the read-only AgentWS viewer; and
- required runtime tools.

AgentWS implementation files MUST be image content. They MUST NOT be supplied
through an overlapping read-only parent bind with writable child overlays.

Every non-override team image MUST be built from its repository Dockerfile. The
completed image MUST preserve:

- `/usr/local/bin/cyclo-container-entrypoint`;
- the inherited OCI health check;
- the exact common-base identity label; and
- a final configured user of root, `0`, or empty.

The entrypoint MUST drop to the mapped non-root host UID/GID before writing any
team-controlled path. Under that identity it MUST copy Cyclo's read-only Pi
settings template into the team's private Pi state and then execute the team
runtime. Cyclo MUST refuse team build or execution when the host UID is zero.
A queue-only one-shot administration mode MAY skip Pi initialization, but only
when Docker starts it directly as the mapped non-root identity and without
receiving a Pi mount. Host UID zero MUST be rejected.

Task administration MUST allowlist the exact AgentWS task programs. A one-shot
task tool MUST have a read-only root filesystem, no network, no project/team/Pi
or credential mount, no Docker socket, no added capability, and no-new-
privileges. It MUST receive only the queue roots needed by its operation:

- list/show: tasks read-only;
- comment/state: tasks read-write; and
- create: tasks and jobs read-write plus a read-only private snapshot of the
  selected specification.

Before staging that snapshot, Cyclo MUST walk and open the host path without
following any symlink, require a regular file, and enforce a fixed size bound.
The one-shot container MUST NOT receive the original project path.

The one-shot container MUST carry an installation ownership label so a later
serialized task operation can remove an abandoned predecessor.

Cyclo MUST build the common team image under its stable tag with the invoking
host UID/GID as build arguments. A derived team image MUST use a stable tag
that includes the canonical team-path identity, receive the common stable tag
as `CYCLO_TEAM_BASE`, and record the exact common immutable image ID in its
validated base-identity label.

## 7. System compilation

### 7.1 Component set

Cyclo MUST emit:

- one `gateway` component;
- one component for each `host.conf` Provider; and
- one stable generated component name for each running instance.

The gateway MUST expose `cyclo.provider.v1.Provider` as `provider` and mount one
DComp named volume at `/var/lib/cyclo-gateway`.

Every team MUST declare one Provider input named `provider` and link it to the
outer Provider.

### 7.2 Generated policy

Cyclo MUST generate only typed DComp policy:

- immutable image;
- input/output descriptors;
- direct links;
- canonical binds;
- named volumes;
- literal command arguments;
- explicit published ports; and
- explicit egress.

Cyclo MUST NOT pass arbitrary Docker arguments through project or host
configuration.

### 7.3 System publication

Cyclo MUST validate the complete in-memory system before writing it.

It MUST write each current component descriptor to a content-addressed
directory below:

```text
STATE_ROOT/system/descriptors/COMPONENT-DIGEST/component.dcomp
```

It MUST then atomically replace:

```text
STATE_ROOT/system/system.dcomp
```

with content that references exactly those descriptors. Once the new system
file is durable, Cyclo MUST remove descriptors that it no longer references.
It MUST NOT retain a generated-system history. Files and containing directories
MUST be flushed so a successful publication survives host failure.

### 7.4 Apply

The apply sequence is:

1. acquire the Cyclo installation lock;
2. validate persisted running-instance mount authority;
3. run the required host Docker builds;
4. compile and atomically publish the complete DComp system;
5. run `dcomp check`;
6. inspect `dcomp status --json`;
7. if DComp reports an unfinished operation, run `dcomp resume`;
8. run `dcomp up`; and
9. wait only for Docker health transitions to settle for a bounded interval.

Cyclo MUST NOT speculate about Docker effects or implement a parallel rollback.
DComp owns resumption and exact-object reconciliation.

If the wait expires while a component is still starting, Cyclo MUST report the
observed state. It MUST NOT destroy the component merely because the wait
expired.

## 8. Network and interface model

### 8.1 DComp links

Each direct input link MUST produce a private internal network containing its
consumer and producer. DComp MUST inject the producer address as:

```text
DCOMP_LINK_<INPUT>=dns:///PRODUCER:50051
```

Cyclo components MUST listen on `0.0.0.0:50051` for their declared ConnectRPC
interfaces.

The link is a network capability, not a one-way firewall or cryptographic
principal. Both attached containers are network peers.

### 8.2 Egress and publication

Cyclo MUST grant:

- gateway egress;
- outer-Provider egress plus a dynamic `127.0.0.1:0 -> 50051` publication;
- normal-team egress plus a configured host publication to container port
  4137; and
- no team egress or publication when `--offline` is selected.

The private team-to-Provider link MUST remain present in offline mode.

Non-outer intermediate Providers MUST not receive an external base route from
Cyclo.

The host Provider client MUST call only `127.0.0.1` and MUST discover the
current dynamic port from DComp status.

### 8.3 Health

Every image MUST define a meaningful OCI health check. DComp reports Docker's
container and health states; it does not mandate a health wire protocol.

Cyclo's built-in components MAY expose
`cyclo.component.v1.Component.Health` and use it from their OCI health check.
External components MAY implement a different bounded local probe.

Cyclo considers a component ready only when DComp reports it running and
healthy.

## 9. Provider protocol

### 9.1 Control plane

`cyclo.provider.v1.Provider.ListModels` MUST return a typed catalogue with
unique public `PROVIDER/MODEL` identifiers and an `inference_format` for each
model.

The Pi boundary MUST reject models whose format is not the pinned Pi ABI without
discarding unrelated valid catalogue entries.

### 9.2 Data plane

`InferRequest` MUST contain:

- the exact selected public model ID; and
- one string payload.

Each streamed `InferResponse` MUST contain one string payload.

For the Pi ABI, the strings encode native Pi JSON call frames and assistant
events. A transparent Provider MUST forward those strings without parsing,
validating, normalizing, or reserializing them.

Components explicitly designed for policy, audit, fusion, or transformation MAY
inspect payloads. Their behavior MUST be treated as part of their component
contract, not the base transport.

Cancellation and deadlines MUST use ConnectRPC transport semantics rather than
fields inside the opaque payload.

### 9.3 Route failure

The outer Provider selected by `host.conf` is authoritative. Cyclo MUST NOT
select a different route when it fails.

A failed Provider MUST remain visible in component status and MUST make the
DComp system non-operational.

## 10. Gateway administration

### 10.1 Service lifecycle

The long-running gateway is an ordinary DComp component. Only DComp may create,
replace, restart, or remove that service container.

### 10.2 One-shot tools

Login, provider enumeration, and usage inspection MAY run the gateway image as
one-shot Docker containers. These are administrative tools, not
components.

Cyclo MUST:

- give every tool an installation-scoped ownership label and unique name;
- remove abandoned labeled tool containers before starting another;
- use `--rm`;
- mount the credential volume only when required;
- obtain its physical Docker name from DComp's verified volume lookup;
- mount it read-only for usage inspection;
- use no network for API-key login and provider enumeration;
- grant ordinary bridge access only to interactive login flows; and
- never print an API key supplied through an environment variable.

### 10.3 Login ordering

Gateway login MUST:

1. build the gateway image through Docker;
2. if necessary, apply only the fixed gateway component so DComp creates and
   verifies the credential volume;
3. complete and commit the credential update;
4. restart only the gateway component; and
5. require the gateway to become running and healthy.

If a complete desired system already exists, login MUST verify its DComp-owned
gateway container and credential volume rather than applying unrelated
Provider or team components.

If the host fails after credential commit, the committed store remains valid.
A later `models`, `repair`, or other apply operation MUST recover service state.

Destroying the store MUST resolve the existing installation gateway volume
through DComp, require exact confirmation of the returned name, stop the
complete DComp system, and then delete that volume. It MUST NOT reapply a
stopped gateway merely to destroy a surviving credential volume. Ordinary
stop, refresh, repair, or forget MUST NOT delete credentials.

## 11. Team runtime

### 11.1 Mounts

Each team component MUST receive:

```text
STATE_ROOT/instances/ID/agentws-state/tasks  -> /agentws/tasks       rw
STATE_ROOT/instances/ID/agentws-state/jobs   -> /agentws/jobs        rw
STATE_ROOT/instances/ID/agentws-state/agents -> /agentws/agents      rw
generated project.cyclo                      -> /agentws/project.cyclo ro
generated Pi settings template               -> /opt/cyclo/pi-settings.json ro
STATE_ROOT/instances/ID/pi                   -> /home/cyclo/.pi       rw
team repository                              -> /team                 ro|rw
project rw mount                             -> /workspace/NAME       rw
project ro mount                             -> /readonly/NAME        ro
```

Bind and volume targets within a component MUST NOT overlap.

### 11.2 Generated project view

Cyclo MUST generate one immutable, instance-specific project file containing
the authored name, description, context, selected team mode, and every declared
mount rewritten to container paths.

Generated structured fields MUST NOT expose host paths. Authored free text is
literal and MUST be documented as unsuitable for secrets.

### 11.3 Supervisor

The team supervisor MUST:

- require a valid `/agentws/project.cyclo`;
- require a direct roster file below `/team`;
- acquire an exclusive queue lifetime lock;
- reset orphaned active jobs before starting workers;
- start the AgentWS queue runner and read-only viewer;
- report a child exit as a component failure; and
- terminate all child process groups with a bounded grace period.

The OCI health check MUST test the local AgentWS viewer on port 4137.

## 12. Mount authority

Cyclo MUST resolve each selected team or project source to a canonical existing
directory. Within one project it MUST reject duplicate or overlapping sources.
Across running projects it MUST reject strict ancestor/descendant relationships.
Exact reuse of the same team root or the same project root is permitted; a team
root and project root MUST remain separate even when their paths are identical.

It MUST also reject overlap with:

- the Cyclo state root;
- the installed Cyclo package;
- the selected `host.conf`;
- the DComp executable;
- host Pi configuration;
- `/proc`, `/sys`, `/dev`, and `/run`; and
- known Docker socket locations, including the bound endpoint.

Before an initial instance is committed, Cyclo MUST record and recheck the
device/inode identity of every team and project bind source.

Before every later system compilation, Cyclo MUST re-resolve and revalidate all
persisted running-instance mount sources.

## 13. Persistent state

### 13.1 Cyclo state

Cyclo state MUST use private directories and atomic regular files:

```text
STATE_ROOT/
  host-config.scope
  docker-endpoint
  control.lock
  pending-instance-batch.json
  instances/ID/
    run.json
    agentws-state/{tasks,jobs,agents}/
    pi/
    project-config/GENERATION/project.cyclo
    runtime-config/CONTENT_DIGEST/pi-settings.json
  system/
    system.dcomp
    descriptors/
  dcomp/
```

`run.json` MUST contain domain facts only:

- logical instance ID;
- team/project identities and generations;
- exact immutable team image ID;
- image override, model list, and runtime options;
- project description and generated project content;
- normalized mount source facts;
- `running` or `stopped` intent; and
- schema/runtime version.

It MUST NOT contain container IDs, network IDs, published observed ports, or
DComp operation state.

State reads MUST reject unknown fields, unsupported schemas, invalid types,
symlinked entries, and malformed project metadata.

State replacement MUST use same-filesystem temporary files, `fsync`, atomic
rename, and parent-directory synchronization.

A multi-instance mutation MUST durably journal the complete replacement cohort
before the first `run.json` replacement. Every inventory reader MUST serialize
with that publication and complete a valid interrupted cohort before returning
state. Invalid existing metadata MUST be preserved and reported, never silently
overwritten.

### 13.2 DComp state

The contents and schema of `STATE_ROOT/dcomp` belong exclusively to DComp.
Cyclo MUST NOT import or parse those files. It MUST use `version --json`,
`status --json`, `volume --json`, and the documented DComp command exit
contracts.

### 13.3 Gateway state

Credentials and usage MUST live in the installation-scoped named Docker volume.
DComp down/replacement MUST preserve declared volumes. Only the explicit
destroy-store operation may delete it.

## 14. Lifecycle operations

### 14.1 Serialization

All Cyclo mutations of installation state or runtime composition MUST hold one
exclusive host file lock. Read-only status operations SHOULD avoid that lock.

### 14.2 Run

`cyclo run PROJECT` MUST:

1. parse and validate the current project and teams;
2. validate mount authority;
3. apply the current host Provider system;
4. query and validate the outer catalogue;
5. build or validate each selected team image;
6. recheck bind-source identity;
7. atomically persist every new instance with `running` intent; and
8. apply the complete system including those teams.

Existing instance IDs MUST fail rather than be overwritten.

If the final apply fails, committed running intent MUST remain visible and
repairable.

Before reporting success, `run` MUST observe no pending DComp operation and
require the outer Provider plus every newly requested team component to be
running and healthy. Failure of an unrelated optional component MUST NOT make a
healthy requested team appear to have failed.

### 14.3 Start and stop

`start` MUST persist `running` intent before apply. `stop` MUST persist
`stopped` intent before apply.

Before reporting success, `start` MUST apply the same target-team and outer
Provider readiness rule as `run`.

If apply fails, the durable intent MUST remain the requested value. A later
repair MUST retry reconciliation from that intent.

Stopping a project path applies to teams selected by the currently parsed
project file. Instances removed from that file remain individually addressable
by ID.

### 14.4 Refresh

Refresh MUST:

1. reparse the recorded project file for every running instance;
2. locate the same logical team selection;
3. validate current mounts and build every replacement team image;
4. apply only the current gateway and configured Provider components;
5. obtain the outer Provider catalogue and validate every replacement model;
6. atomically publish the complete running-instance replacement cohort;
7. apply the complete system; and
8. require the outer Provider and every refreshed team to be running and
   healthy before reporting success.

Stopped instances MUST retain their prior persisted configuration and intent.

### 14.5 Repair

Repair MUST run the required host Docker builds, apply current `host.conf` plus
persisted instance intent, and resume incomplete DComp operations.

Repair MUST NOT reparse mutable team/project definitions to rewrite persisted
instances. Refresh owns that adoption boundary.

### 14.6 Forget

Forget MUST require:

- an existing stopped instance; and
- exact repeated confirmation of its ID.

Cyclo MUST first apply the system and verify that stopped teams are absent from
the desired composition. It may then atomically remove the instance directory
and its AgentWS/Pi state.

### 14.7 Component restart

Provider restart MUST first apply the current global composition, verify the
configured Provider components, then ask DComp to restart those committed
components.

Gateway restart MUST prepare only the gateway/store boundary and restart the
committed gateway component. Generic component restart MUST inspect the
committed DComp status, reject an absent component, and restart that exact
component without reconciling `host.conf` or instance intent.

Restart MUST NOT create a parallel container lifecycle path.

## 15. Status and diagnostics

### 15.1 DComp status contract

Cyclo MUST request `dcomp status --json NAME`. Machine API 1 returns the system
name, desired/applied state, operational flag, digest, pending operation,
network diagnostics, component state, Docker health, exit code, problems, and
effective published ports.

DComp exit status 0 MUST correspond to `operational=true`; exit status 1 MUST
correspond to `operational=false`. Cyclo MUST reject a disagreement or malformed
JSON.

### 15.2 Cyclo component condition

Cyclo reports:

- `ready` only for `status=running` and `health=healthy`;
- `absent` when the component is not in the applied system; and
- `not-ready` for every other observed component state.

This condition is not an assertion that every agent is idle, successful, or
able to complete its assigned task.

### 15.3 Observational commands

`ps`, `inspect`, `logs`, `component list/status/logs`, `providers status`,
`gateway status`, `doctor`, and the dashboard MUST derive output from Cyclo
domain state and DComp/Docker facts. They MUST NOT synthesize alternate routes.

Runtime construction, generic component inspection/restart, and gateway
providers/login/status/restart/usage MUST NOT require `host.conf` to parse.
When provider context is unavailable, `ps`, `inspect`, `doctor`, and the
dashboard MUST preserve the applied DComp diagnostics and report the
configuration error separately. Provider compilation, reconciliation, and
provider-specific inspection remain fail-closed on invalid `host.conf`.

`doctor` MUST check:

- DComp executable and machine API compatibility;
- applied-system presence;
- every applied component, plus expected gateway and running-team components;
- every configured Provider component when `host.conf` is valid;
- absence of stopped-team components; and
- outer model catalogue reachability when the system is operational and
  provider configuration is available.

`models` is not observational: it MUST apply the desired system before querying
the catalogue.

### 15.4 Logs

Cyclo MUST delegate long-lived component logs to DComp. Component selection
MUST use exact generated names, not unverified Docker names.

Logs are operational data and may contain prompts, model output, project paths,
or secrets emitted by applications. Cyclo MUST NOT represent them as a secure
audit log.

## 16. Failure behavior

### 16.1 Configuration and build failures

Parsing, mount validation, missing images, build failure, and nominal interface
mismatch MUST fail before DComp mutates the generated target system whenever
possible.

Cyclo MUST preserve already committed instance intent when a later apply fails.

### 16.2 Component failures

A failed component MUST remain in DComp status and logs. Cyclo MUST NOT delete
it merely to make the rest of the system appear operational.

Removing a failed optional Provider requires an explicit `host.conf` change
followed by apply or repair.

### 16.3 Host interruption

DComp MUST record operation intent before Docker mutation and address verified
immutable objects. Cyclo MUST resume an incomplete operation before applying a
new target.

Cyclo MUST NOT infer success or failure from a timeout alone, and MUST NOT
implement speculative rollback around DComp.

### 16.4 One-shot gateway interruption

Every one-shot gateway tool container MUST carry an installation ownership
label. Before the next tool operation, Cyclo MUST enumerate and forcibly remove
abandoned containers with that exact label.

Credential-volume deletion MUST never be part of abandoned-tool cleanup.

## 17. Required security invariants

A conforming Cyclo 0.2 installation MUST preserve all of these:

1. Only the gateway service and bounded gateway administration tools can mount
   the credential volume.
2. No runtime component receives the Docker socket.
3. No runtime component receives Cyclo or DComp host state.
4. A team receives only its team repository, declared project mounts, private
   queue/Pi state, generated project file, and outer Provider link.
5. A Provider receives only its image policy, declared volumes, egress policy,
   and declared links.
6. Host-side Provider RPC is loopback-only.
7. Every DComp runtime image is addressed by immutable ID.
8. Running team workloads as host root is rejected.
9. Protected and overlapping bind sources are rejected.
10. Provider inference payloads are transparent unless an explicit component
    advertises inspection or transformation.
11. Dashboard and viewer non-loopback exposure is explicit and warned as
    unauthenticated.
12. Separate state roots never authorize adoption of each other's DComp-owned
    resources.

## 18. Acceptance criteria

A release implementation SHOULD demonstrate:

### 18.1 DComp boundary

- missing DComp fails clearly;
- an API version other than 1 fails before mutation;
- a bound Docker endpoint is passed consistently;
- malformed or exit-inconsistent status JSON fails closed; and
- interrupted operations resume through DComp.

### 18.2 Composition

- an empty `host.conf` links a team directly to `gateway.provider`;
- a multi-Provider configuration compiles exact endpoint links;
- forward references and cycles validate nominally;
- every missing or duplicate input binding fails;
- the last provider is the host/team outer route; and
- one failed component is reported without changing the configured route.

### 18.3 Images

- repeated operations invoke Docker build with the expected stable tag,
  Dockerfile, context, and `--iidfile`;
- Docker, rather than Cyclo, determines context filtering and cache reuse;
- a mismatch between the `--iidfile` and inspected stable tag fails closed;
- DComp and persisted instances receive only immutable image IDs;
- Cyclo persists no source-digest cache or image-build history;
- a missing health check fails;
- a derived team that changes entrypoint, user contract, or common base fails;
  and
- refresh runs builds before applying replacements.

### 18.4 Team and project isolation

- several writable projects map to distinct `/workspace` children;
- read-only inputs map only below `/readonly`;
- team access follows its independent `ro|rw` mode;
- AgentWS code is baked into the image while only queue children are mounted;
- `/agentws/project.cyclo` contains container paths and no generated host paths;
- protected or nested roots and post-validation symlink/swap attacks fail; and
- offline teams retain their Provider link but have no external route or
  published viewer.

### 18.5 Persistence and failure

- run/start/stop persist intent before final apply;
- failed apply leaves intent inspectable and repairable;
- refresh adopts current running project/team sources;
- repair does not rewrite persisted team/project state from mutable definitions;
- stopped tasks remain inspectable;
- forget requires stopped intent and exact confirmation; and
- a killed one-shot gateway tool is cleaned without deleting credentials.

### 18.6 Security

- team and intermediate Provider containers cannot see the credential volume;
- no component has the Docker socket;
- link networks contain only their declared endpoints;
- host Provider publication is loopback-only;
- normal and offline network policies differ as specified; and
- status/dashboard operations do not mutate composition.
