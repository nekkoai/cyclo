# Cyclo user guide

## 1. Install

Cyclo requires Linux, Python 3.10 or newer, Git, a local Docker Engine, and a
DComp executable whose machine API is version 1.

Install DComp on `PATH`, or point Cyclo at an executable:

```sh
export CYCLO_DCOMP=/opt/dcomp/bin/dcomp
"$CYCLO_DCOMP" version --json
```

The response must contain `"api_version":1`. Cyclo checks this interface before
every new operational client session; it does not parse DComp's terminal
output.

Install Cyclo itself on the host:

```sh
python3 -m pip install .
cyclo --version
```

Run Cyclo as a non-root user who can access the selected Docker daemon. Cyclo
refuses to build or run teams as host root because that would make agent code
root inside its container.

Cyclo supports only a local Docker Unix socket. The first operation that needs
the Docker endpoint records the selected canonical value in the state root.
Later commands reject a different context or endpoint for that installation.

## 2. Select an installation

Without an explicit root, Cyclo stores state under
`${XDG_STATE_HOME:-$HOME/.local/state}/cyclo` and reads
`/etc/cyclo/host.conf`.

For a user-owned installation, set an explicit root:

```sh
export CYCLO_STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/cyclo-work"
mkdir -p "$CYCLO_STATE_ROOT"
```

That installation reads `$CYCLO_STATE_ROOT/host.conf`. The selected
system/local configuration scope is recorded on first mutation. Do not alternate
between an explicit and implicit spelling for the same state directory.

The canonical state-root path determines the installation ID, DComp system
name, Docker resource names, gateway credential volume, generated image names,
and queue locations. Several installations may share one trusted Docker host
when each uses a different state root.

Cyclo 0.2 does not migrate Cyclo 0.1 state. Start with a fresh state root.

## 3. Configure the gateway

List supported login providers before authenticating:

```sh
cyclo gateway providers
```

OAuth or subscription login is interactive:

```sh
cyclo gateway login openai-codex --as codex-work
```

Read an API key without putting it in shell history:

```sh
cyclo gateway login openai --as openai-work --api-key-stdin
```

Or read a named environment variable:

```sh
cyclo gateway login openai --as openai-work --api-key-env OPENAI_API_KEY
```

`--as` chooses the public account/provider prefix used in model IDs. A
successful login writes the gateway's private Docker volume and restarts the
gateway. On a fresh installation Cyclo first creates only that fixed
gateway/store boundary; unrelated Provider or team failures do not block login.
A separate build or restart command is not required.

Inspect the result:

```sh
cyclo gateway status
cyclo models
cyclo usage
```

Useful gateway operations are:

```sh
cyclo gateway restart
cyclo gateway build
cyclo gateway destroy-store --confirm VOLUME
```

`build` explicitly runs the gateway Docker build, applies the complete system,
and restarts the gateway. Other operations that need the gateway image also
invoke Docker build; Docker decides layer-cache reuse. `destroy-store` is
destructive: first copy the exact volume name reported by
`cyclo gateway status`.

## 4. Configure Provider components

The gateway is always present and exposes `gateway.provider`. If `host.conf` is
absent or empty, it is also the outer Provider.

Install an intermediate component by adding one line:

```text
provider trace ./providers/passthrough upstream=gateway.provider -- label=trace
```

The complete syntax is:

```text
provider NAME SOURCE [context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARGUMENT ...]
```

Rules:

- `NAME` is a lower-case DComp component name.
- Relative `SOURCE` paths resolve beside `host.conf`; `~` is not expanded.
- `SOURCE` must contain `component.dcomp`.
- `context=PATH` optionally selects a containing Docker build context.
- Every input declared by `component.dcomp` must be bound exactly once.
- A binding names a concrete `COMPONENT.OUTPUT`; the service identities must
  match.
- All declarations are resolved together, so forward references and cycles are
  valid address wiring.
- Words after `--` replace the image's command arguments. There is no quoting
  or shell evaluation.
- The last provider line selects the outer Provider used by teams and host
  catalogue calls.

Example descriptor:

```text
docker cyclo-passthrough:dev
input cyclo.provider.v1.Provider upstream
output cyclo.provider.v1.Provider provider
```

If `SOURCE/Dockerfile` exists, Cyclo builds it. Otherwise the descriptor's
Docker image must already exist locally. Every component image must define an
OCI health check.

Inspect provider configuration and runtime status:

```sh
cyclo providers check
cyclo providers status
cyclo providers restart
cyclo component list
cyclo component status trace
cyclo component logs -f trace
cyclo component restart trace
```

Provider status is literal. A broken component is not bypassed. Fix or remove
it, then apply the system again.

## 5. Create a team

List the bundled starting points:

```sh
cyclo team templates
```

Create a Git repository:

```sh
cyclo team init ./teams/jon-rtl --template plan-execute-verify --model codex-work/MODEL
```

The repository contains:

```text
jon-rtl/
  team
  roles/
    planner.md
    implementer.md
    verifier.md
```

It may add `AGENTS.md` for team-wide instructions and a `Dockerfile` for extra
packages. The roster format is:

```text
NAME ROLE ENGINE PROVIDER/MODEL
```

Cyclo currently supports `pi` and `pi-interactive`. Every role needs a matching
`roles/ROLE.md`, names must be unique, and at least one agent must have the
`planner` role.

Validate a team:

```sh
cyclo validate ./teams/jon-rtl
```

Teams requiring extra tools inherit the common runtime:

```dockerfile
ARG CYCLO_TEAM_BASE
FROM ${CYCLO_TEAM_BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends verilator
```

The final stage must use `CYCLO_TEAM_BASE` and preserve Cyclo's entrypoint and
health check. Team and provider Dockerfiles are trusted host build inputs;
review them before running Cyclo.

## 6. Define a project

Create a context file that explains the source layout, then generate a project:

```sh
cyclo project init ./project.cyclo --context ./project-context.md --team ./teams/jon-rtl ro --mount core-et /home/user/openhw/core-et rw --mount specifications ./specifications ro
```

The generated format is intentionally small:

```text
name core-et-uart
description Implement and verify an ET-Link UART.
context <<PROJECT_CONTEXT
`core-et` is the writable implementation repository.
`specifications` contains read-only protocol documentation.
PROJECT_CONTEXT
team ./teams/jon-rtl ro
mount core-et /home/user/openhw/core-et rw
mount specifications ./specifications ro
```

Every team line becomes an independent persisted instance. Every selected team
sees all project mounts:

```text
/workspace/core-et
/readonly/specifications
```

`rw` mounts are projects. `ro` mounts are supporting inputs. A definition may
contain several of each. The team repository itself is mounted at `/team` with
the mode on its `team` line.

Cyclo also creates a read-only `/agentws/project.cyclo` for each team. It keeps
the name, description, and context but replaces host paths with the actual
container paths. Agents are required to read it before choosing a workspace.

Validate without starting anything:

```sh
cyclo validate ./project.cyclo
```

## 7. Run work

Start every team in a project:

```sh
cyclo run ./project.cyclo
```

Common options are:

```text
--image IMAGE       use one prebuilt compatible team image
--offline           remove direct team egress and dashboard publication
--host IPV4         AgentWS viewer bind address; default 127.0.0.1
--port PORT         fixed viewer port for a single-team project; default dynamic
--verbose           mirror rendered agent transcripts to component logs
--foreground        follow logs after a single-team start
```

`--port` and `--foreground` are rejected for multi-team projects.
`--port` is incompatible with `--offline`.

`run` performs one global apply:

1. validate project, team, model, mount, and Docker authority;
2. build the gateway and configured source-built providers through Docker;
3. build team images, allowing Docker to reuse cached layers;
4. persist each new instance with `running` intent; and
5. compile and apply gateway, providers, and every running team through DComp.

If apply fails after intent is persisted, the instances remain visible and
repairable. Fix the reported component or configuration, then run:

```sh
cyclo repair
```

## 8. Tasks

Create a task from a specification file:

```sh
cyclo task run INSTANCE uart-ip ./uart-task.md
```

Inspect and update it:

```sh
cyclo task list INSTANCE
cyclo task show INSTANCE uart-ip
cyclo task comment INSTANCE uart-ip "Review requested"
cyclo task complete INSTANCE uart-ip -m "Accepted"
cyclo task reopen INSTANCE uart-ip -m "More work required"
```

Task operations run the bundled AgentWS tools in a confined one-shot container
over the host-owned durable queues. They remain available when a team component
is stopped; a stopped team simply does not process pending work until it is
started again.

Print the durable queue path when direct local inspection is needed:

```sh
cyclo path INSTANCE
```

Use AgentWS tools rather than editing queue control files by hand.

## 9. Observe the system

Inspect teams:

```sh
cyclo ps
cyclo inspect INSTANCE
cyclo logs INSTANCE
cyclo logs -f INSTANCE
```

Inspect the global DComp composition:

```sh
cyclo component list
cyclo component status
cyclo component logs -f COMPONENT
cyclo doctor
```

`doctor` is observational. It checks DComp compatibility, expected components,
Docker health, stopped-instance absence, and the model catalogue when the
system is operational. It does not build or apply the system.

Serve the fleet dashboard:

```sh
cyclo dashboard --host 127.0.0.1 --port 8080
```

The fleet dashboard and each AgentWS viewer are read-only and unauthenticated.
Keep them on loopback unless a trusted network boundary or authenticated
reverse proxy protects them. When a viewer is bound to `0.0.0.0`, browser links
use the host of the incoming request, not the wildcard bind address.

## 10. Lifecycle

Stop one instance or all current teams listed by a project:

```sh
cyclo stop INSTANCE
cyclo stop ./project.cyclo
```

Start a stopped instance from its persisted definition:

```sh
cyclo start INSTANCE
```

Adopt current project/team sources and rebuild every running instance:

```sh
cyclo refresh
```

`refresh` reparses each running instance's recorded project file, validates its
current team, runs host and team Docker builds, updates the persisted instance,
and applies the installation-wide DComp system. Stopped instances remain
stopped and retain their previous persisted configuration.

Reapply current `host.conf` and persisted instance intent:

```sh
cyclo repair
```

`repair` runs the required host Docker builds and resumes an interrupted DComp
operation before applying. It does not re-read team/project definitions for
persisted instances; use `refresh` for that.

Delete a stopped instance and its durable AgentWS/Pi state:

```sh
cyclo forget INSTANCE --confirm INSTANCE
```

Cyclo first applies the system without that stopped team, then removes the
instance directory. Gateway credentials and declared DComp volumes are not
removed by ordinary stop, refresh, repair, or forget operations.

## 11. State and ownership

A typical explicit installation contains:

```text
STATE_ROOT/
  host.conf
  host-config.scope
  docker-endpoint
  control.lock
  pending-instance-batch.json  # present only during recoverable cohort publish
  instances/
    INSTANCE/
      run.json
      agentws-state/
        tasks/
        jobs/
        agents/
      pi/
      project-config/
      runtime-config/
  system/
    system.dcomp
    descriptors/
  dcomp/
```

Cyclo owns instance intent, queue state, Pi state, image construction, and the
generated system definition. DComp owns the contents of `dcomp/` and all
container/network/volume lifecycle state. Cyclo never reads DComp's private
files; it uses machine API version 1. The gateway owns credentials and usage in
its named Docker volume.

When one command changes several instances, Cyclo journals the complete cohort
in `pending-instance-batch.json` before replacing any `run.json`. Readers hold
the same installation lock and finish an interrupted publication before
returning an inventory snapshot.

Do not copy one state root over another running installation. Back up state only
with the corresponding system stopped and include the gateway volume separately
when credential recovery is required.

## 12. Mount and network safety

Cyclo rejects:

- missing, non-directory, or non-canonical bind sources after path resolution;
- overlapping team and project trees;
- mounts overlapping Cyclo state, installed Cyclo code, `host.conf`, DComp,
  the host Pi directory, `/proc`, `/sys`, `/dev`, `/run`, or a Docker socket;
- a selected Docker endpoint that is remote or changes after installation
  binding; and
- team image execution as host root.

Team containers never receive the Docker socket or gateway credential volume.
Provider links are private DComp networks. This isolation does not make
readable project data confidential from a normal network-enabled team or from
the external model service. Use `--offline`, separate projects/installations,
or explicit policy components according to the deployment threat model.

## 13. Troubleshooting

Check the boundary first:

```sh
"${CYCLO_DCOMP:-dcomp}" version --json
cyclo doctor
cyclo component status
```

If DComp reports an interrupted operation, ordinary mutating Cyclo commands and
`cyclo repair` attempt `dcomp resume` before applying the desired system.

If one provider is unhealthy:

```sh
cyclo component logs PROVIDER
cyclo component status PROVIDER
```

Correct its source, image, arguments, or `host.conf` binding. The configured
outer Provider remains the only route.

If a team is unhealthy:

```sh
cyclo inspect INSTANCE
cyclo logs -f INSTANCE
```

The team health check covers the AgentWS viewer. Agent startup and task failures
remain in the component logs and durable AgentWS state.
