<img src="docs/assets/banner.svg" alt="cyclo — Agentic systems, in a Git loop. A local-first agent runtime. V0.2.0, MIT, Linux, Python 3.10+, DComp." width="100%">

Cyclo runs Git-defined agent teams against explicitly mounted projects. A
credential gateway owns provider logins, optional provider components transform
or route model traffic, and each team runs as an isolated component with its
own durable AgentWS queue.

Cyclo uses [DComp](https://github.com/glguida/dcomp) for Docker lifecycle and
component links. Cyclo builds images and compiles one installation-wide DComp
system; DComp reconciles its containers, networks, volumes, and crash-recovery
state. Neither program is in the inference data path after startup.

## 01 · Requirements

- Linux;
- Python 3.10 or newer;
- Git;
- a local Docker Engine reachable through a Unix socket; and
- a `dcomp` executable with machine API version 1.

Install DComp on `PATH`, or set `CYCLO_DCOMP` to its executable. Verify the
machine interface before using Cyclo:

```sh
"${CYCLO_DCOMP:-dcomp}" version --json
```

Cyclo refuses to build or run team workloads as host root. Run it as the user
who owns the project files and may access Docker.

Cyclo 0.2 is a fresh-install boundary. It does not import Cyclo 0.1 state or
Docker resources.

## 02 · First installation

Install the Python package on the host and select a state root:

```sh
python3 -m pip install .
export CYCLO_STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/cyclo-work"
mkdir -p "$CYCLO_STATE_ROOT"
```

An explicit state root uses `$CYCLO_STATE_ROOT/host.conf`. An implicit default
state root uses `/etc/cyclo/host.conf`. The file may be absent or empty, which
routes teams directly to the gateway.

Inspect login providers, authenticate, and list the resulting models:

```sh
cyclo gateway providers
cyclo gateway login openai-codex --as codex-work
cyclo models
cyclo doctor
```

Login stores credentials in the gateway's private Docker volume and restarts
the gateway so the new catalogue is visible. On a fresh installation Cyclo
creates only that fixed gateway/store boundary first; unrelated Provider or
team failures cannot block login. API keys and OAuth sessions are never mounted
into provider or team components.

## 03 · Provider composition

The gateway is always the root Provider. Each non-gateway provider is an
ordinary source directory containing `component.dcomp` and, when Cyclo should
build it, a `Dockerfile`:

```text
# providers/passthrough/component.dcomp
docker cyclo-passthrough:dev
input cyclo.provider.v1.Provider upstream
output cyclo.provider.v1.Provider provider
```

Install component instances in `host.conf`:

```text
provider trace ./providers/passthrough upstream=gateway.provider -- label=trace
provider policy ./providers/policy upstream=trace.provider
```

The grammar is:

```text
provider NAME SOURCE [context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARGUMENT ...]
```

Relative `SOURCE` paths resolve beside `host.conf`. `context=PATH` selects a
larger Docker build context containing the source. Every declared input must be
bound exactly once to an output with the same protobuf service name. All
declarations are resolved together, so links may refer to components written
later in the file. The last provider declaration is the outer Provider used by
teams and host-side catalogue calls; with no declarations, `gateway.provider`
is outer.

Cyclo uses stable installation/version tags for built gateway, Provider, and
team images. Whenever an operation needs one, Cyclo invokes `docker build` with
the real source context and lets Docker apply `.dockerignore` and its native
layer cache. Cyclo captures the build output, inspects the resulting immutable
image ID, and gives only that ID to DComp. It keeps no source-digest cache or
build history.

DComp gives each direct interface link a private internal TCP network.
Components receive only the targets for their declared inputs, such as
`DCOMP_LINK_UPSTREAM=dns:///trace:50051`.

## 04 · Teams and projects

Create a team repository and a project definition:

```sh
cyclo team init ./teams/jon-rtl --template plan-execute-verify --model codex-work/MODEL
cyclo project init ./project.cyclo --context ./project-context.md --team ./teams/jon-rtl ro --mount core-et /home/user/openhw/core-et rw --mount specifications ./specifications ro
```

A project is a small line-oriented file:

```text
name rtl-work
description Integrate a reusable UART IP into CORE-ET.
context <<PROJECT_CONTEXT
`core-et` is the writable implementation repository.
`specifications` contains normative read-only interface documentation.
PROJECT_CONTEXT
team ./teams/jon-rtl ro
mount core-et /home/user/openhw/core-et rw
mount specifications ./specifications ro
```

Every `team` line creates one durable Cyclo instance and one generated DComp
team component. Writable mounts appear at `/workspace/<name>`; read-only inputs
appear at `/readonly/<name>`. Several writable mounts represent several
projects. The team repository is mounted at `/team` with its declared mode.

Cyclo bakes AgentWS, Pi, the provider adapter, and the generic agent protocol
into the common team image. At runtime it mounts only durable queue directories,
Pi state, the team repository, the generated read-only
`/agentws/project.cyclo`, and declared project paths. Every team repository
contains a Dockerfile derived from Cyclo's standard team-component image:

```dockerfile
ARG CYCLO_TEAM_BASE
FROM ${CYCLO_TEAM_BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends verilator
```

The two-line `ARG`/`FROM` form is sufficient when no extra packages are needed.
Edit it to install the team's tools. Cyclo supplies `CYCLO_TEAM_BASE`; the final
stage must inherit it and preserve Cyclo's entrypoint and health check.

Run and inspect the project:

```sh
cyclo validate ./project.cyclo
cyclo run ./project.cyclo
cyclo ps
cyclo inspect INSTANCE
cyclo task run INSTANCE uart-ip ./uart-task.md
cyclo task add-job INSTANCE uart-ip uart-ip-builder-r1 builder ./recovery.md
cyclo task list INSTANCE
cyclo logs -f INSTANCE
cyclo dashboard --host 127.0.0.1
```

Jobs are routed by role. A team roster declares
`NAME ROLE ENGINE PROVIDER/MODEL`; the role selects both the queue work an agent
may claim and its `roles/ROLE.md` instructions. Several agents may share a role.
Task coordination authority belongs to role `planner`.

Use `cyclo refresh` to re-read running projects and teams, rebuild their images,
and apply the resulting installation-wide DComp system. Use `cyclo stop`,
`cyclo start`, and `cyclo forget` for durable instance intent. `cyclo repair`
runs the required host Docker builds, reapplies the current host configuration
and persisted instance intent, and resumes an interrupted DComp operation.

## 05 · Runtime model

One canonical state root defines one Cyclo installation and one DComp system:

```text
Cyclo CLI
  ├── runs Docker builds and validates immutable image IDs
  ├── stores project/team intent and AgentWS queues
  └── compiles gateway + providers + running teams
                         │
                         v
                       DComp
  └── owns containers, link networks, volumes, and operation recovery
```

The gateway and outer Provider have explicitly published loopback endpoints for
host administration and model discovery. Team dashboards are published on the
address requested by `cyclo run`; the default is `127.0.0.1`. Normal teams have
external network access. `--offline` removes that access and the dashboard
publication while retaining the private Provider link.

The configured outer Provider is the only route. A failed component remains
inspectable through `cyclo component status` and makes the system
non-operational until it is fixed or removed from `host.conf`.

## 06 · Multiple installations

Use a different state root for each installation:

```sh
CYCLO_STATE_ROOT="$HOME/.local/state/cyclo-work" cyclo doctor
CYCLO_STATE_ROOT="$HOME/.local/state/cyclo-lab" cyclo doctor
```

The canonical state-root path determines the DComp system name, Docker resource
namespace, image names, gateway volume, queues, and generated configuration.
Each installation binds itself to the selected local Docker Unix socket on its
first operation that needs a Docker endpoint and rejects later attempts to
retarget it.

## 07 · Documentation

- [Architecture](docs/architecture.md)
- [Operations guide](docs/guide.md)
- [Provider protocol](docs/provider-protocol.md)
- [Project format](docs/project-format.md)
- [Team repository contract](docs/team-repositories.md)
- [Security policy](SECURITY.md)

Cyclo ships its gateway, provider protocol, team runtime, AgentWS runtime,
dashboard, and team templates. DComp is a separate required executable;
external `agentws` and `multiagent` checkouts are not runtime dependencies.

<img src="docs/assets/fregio.svg" alt="cyclo · MIT licence" width="100%">
