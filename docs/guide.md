# Cyclo user guide

Cyclo runs Git-defined agent teams against directories selected by a
`project.cyclo` file. Each team gets an isolated Docker container, durable
AgentWS queue, writable Pi state, declared project mounts, and read-only access
to the outer model-provider Unix socket. Credentials remain in the independent
gateway volume.

For design details, see [Architecture](architecture.md). For the exact project
grammar, see [Project format](project-format.md).

## Requirements

- Linux
- Python 3.10 or newer
- Git
- Docker, with a running daemon accessible to the current user

Node.js and npm are maintainer requirements, not host runtime requirements.
The installed Python package contains the component, gateway, Pi, team, and
AgentWS sources used to build its images.

Cyclo 0.2 is a fresh-install boundary: it does not adopt or migrate 0.1 state
or Docker resources. Before the first 0.2 command, select a new state root and
keep it set for every command:

```sh
export CYCLO_STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/cyclo-0.2"
```

Do not point 0.2 at a state root used by 0.1. Then install from a source
checkout or release artifact using the Python environment policy appropriate
for the machine:

```sh
python3 -m pip install .
cyclo --version
cyclo doctor
```

The distribution is named `cyclo-agent`; the command and Python package are
named `cyclo`.

## First gateway

List login choices before storing any credential:

```sh
cyclo gateway providers
```

The validated official tag is the installed gateway image. Provider discovery,
login, and start reuse a valid current-release image, building only when it is
absent or from a different release. Restart requires an already-installed
current image and never builds. `cyclo gateway build` explicitly rebuilds and
restarts the gateway. The private credential volume is created if absent and
survives ordinary build, stop, and restart operations.

Login using OAuth/subscription or an API key:

```sh
cyclo gateway login openai-codex --as codex-work
cyclo gateway login anthropic --as claude-work
cyclo gateway login openai --as openai-work --api-key-stdin
cyclo gateway login openai --as openai-work --api-key-env OPENAI_API_KEY
```

The account name selected by `--as` becomes the public prefix in
`ACCOUNT/MODEL`. Account names are 1–64 lowercase letters, numbers,
underscores, or hyphens, begin with a letter or number, and may not use a
reserved Cyclo route name. Login writes the store, restarts the gateway, and
returns only after the resulting model catalogue is ready:

```sh
cyclo models
```

The gateway commands are:

```text
cyclo gateway providers
cyclo gateway login PROVIDER [--as ACCOUNT] [--api-key-stdin|--api-key-env NAME]
cyclo gateway build
cyclo gateway start
cyclo gateway restart
cyclo gateway stop
cyclo gateway status
cyclo gateway destroy-store --confirm VOLUME
```

`gateway build` is the source-update boundary. The other gateway commands do not
compare the installed image with its source. Cyclo keeps no source hash or cache
database; Docker applies `.dockerignore` and layer-cache rules during an
explicit build. After changing gateway source, use `cyclo gateway build` or
`cyclo refresh`.

`destroy-store` is the explicit destructive operation for credentials and
usage. `cyclo gateway status` prints `VOLUME`; no other gateway command deletes
it.

## Provider components

The gateway is the fixed root Provider. Optional intermediate providers are an
ordered component graph declared by the installation's `host.conf`. The
implicit state root initially uses `/etc/cyclo/host.conf`; a state root selected
explicitly uses `STATE_ROOT/host.conf`. Cyclo keeps that association with the
installation after the provider graph is first applied:

```text
# provider INSTANCE SOURCE [context=PATH] REQUIREMENT=TARGET ... [-- ARGUMENT ...]
provider first ./providers/passthrough upstream=gateway
provider second ./providers/passthrough upstream=first
```

`first` and `second` are ordinary host-local instance names; they have no
special meaning. Each `SOURCE` directory contains:

```text
Dockerfile
component.conf
```

For example:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

Requirement names in `component.conf` are bound with `NAME=TARGET` in
`host.conf`. A target is `gateway` or an earlier component instance. The file
order is therefore the dependency order; forward references fail. Arguments
after `--` explicitly replace the image's OCI `CMD`; when they are absent,
Docker uses the image's own command unchanged. Cyclo does not require a
component command named `serve`. A `context=PATH` setting selects a Docker
build context containing the component source.

An absent or empty `host.conf` is valid and exposes the gateway directly.
Relative source paths resolve from the configuration file's directory.

Inspect or operate one component directly:

```sh
cyclo component list
cyclo component status [NAME]
cyclo component build NAME
cyclo component start NAME
cyclo component restart NAME
cyclo component stop NAME
cyclo component logs NAME
```

`list` and `status` report each component independently. They show whether its
container is present, current, and running, together with Docker health,
Component health, and a concrete error. They do not compute a graph-wide
readiness flag. A named `status` inspects only that component, so an unrelated
broken container cannot prevent diagnosis. `cyclo component status gateway`
remains usable even when `host.conf` is invalid.

Operate every configured provider with:

```sh
cyclo providers check
cyclo providers build
cyclo providers start
cyclo providers restart
cyclo providers status
cyclo providers stop
```

The validated official tag is the installed component. `component build` and
`providers build` submit the selected contexts to Docker, validate each
completed immutable image, and only then promote that tag; they do not restart
containers. `start`,
`models`, and project `run` reuse valid current-release installed images,
building only missing images or images from a different release. `restart`
requires installed current images and recreates containers without building.
Use `cyclo refresh` after changing installed component source.
Global refresh selects instances whose durable intent is `running`; it never
turns a stopped instance back on. It builds team images and refreshes the
independent provider system, obtains its model catalogue, and validates every
selected team before stopping any team. If a later stop, start, or host process
fails, the running intent remains recorded. Correct the underlying problem and
run `cyclo refresh` again, or use `cyclo repair` to recreate missing running
instances from their current `project.cyclo` definitions. There is no separate
refresh plan to resume or abort.

`stop` removes all provider-lifecycle containers owned by this Cyclo state root
even if the current configuration is temporarily invalid. The gateway remains
independent. Team images have a separate lifecycle: an ordinary project run
still submits the selected common and derived team contexts as described below.

Every provider component gets only its own output socket directory and the
read-only socket directories named by its requirements. Intermediate
components run with no network. Cyclo selects the last working provider whose
inputs are working and mounts that socket read-only into newly started teams.
If a component cannot build or start, Cyclo reports it, skips its dependants,
and keeps an earlier working provider available; this may be the gateway.
Catalogue selection also tries usable components from outermost to innermost:
if a health-ready component cannot return a structurally valid `ListModels`
response, model listing and a new project fall back without retrying inference
requests. The catalogue itself is format-neutral. A project run separately
checks that every model requested by its current Pi team is Pi-compatible.

The Provider data plane carries `model` and an opaque Pi JSON string. Relays do
not understand prompts, history, tools, JSON Schema, or events. See
[Provider protocol v1](provider-protocol.md).

## Create a team

List installed templates:

```sh
cyclo team templates
```

Create a team repository using a Pi-compatible model from `cyclo models`:

```sh
cyclo team init ./teams/my-team --template plan-execute-verify --model codex-work/MODEL_ID
```

`cyclo models` prints the selected Provider catalogue, including models for
future inference formats. `cyclo run` fails before starting containers if a
Pi-based roster selects a model with an incompatible format or metadata.

The repository contains:

```text
team
roles/
  planner.md
  ...
AGENTS.md          # optional team-wide additions
Dockerfile         # optional packages/tools, derived from CYCLO_TEAM_BASE
```

The roster format is:

```text
AGENT ROLE ENGINE PROVIDER/MODEL
```

Supported engines are `pi` and `pi-interactive`. Each role needs a matching
`roles/ROLE.md`. At least one agent must have role `planner` because new tasks
begin with a planner job.

Cyclo supplies AgentWS, Pi, and the provider extension. The team repository
defines the roster, prompts, optional instructions, and—when needed—an
execution-image delta:

```dockerfile
ARG CYCLO_TEAM_BASE=cyclo-team-base-required
FROM ${CYCLO_TEAM_BASE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends verilator yosys \
    && rm -rf /var/lib/apt/lists/*
```

Cyclo uses the common runtime directly when this file is absent. When it is
present, every project run submits the team build context to Docker; Docker
reuses cached work when the effective context is unchanged. Validate the
repository with:

```sh
cyclo validate ./teams/my-team
```

After upgrading Cyclo or changing installed component or team source, use one
command to stop running project instances, rebuild and restart the gateway and
configured providers, then rebuild the selected common and derived team images
while recreating every instance whose recorded intent is `running`:

```sh
cyclo refresh
```

Queue state and lifecycle intent are preserved. Every running instance must
retain its `project.cyclo` path so Cyclo can reproduce the same mount and team
authority. If an operation is interrupted, `cyclo repair` recreates missing
running containers, removes containers for stopped instances, and finishes
pending deletion.

## Define a project

Create a validated one-team definition spanning two writable projects and one
read-only input:

```sh
cyclo project init ./project.cyclo --context ./project-context.md --team ./teams/jon-rtl ro --mount core-et /home/user/openhw/core-et rw --mount uart-ip /home/user/openhw/uart-ip rw --mount specifications ./references/specifications ro
```

Add further `--team PATH MODE` or `--mount NAME PATH MODE` options as needed.
Alternatively, write the same format by hand:

Create `project.cyclo`:

```text
name core-et-uart
description Integrate and verify a reusable UART IP in CORE-ET.

context <<PROJECT_CONTEXT
`core-et` is the processor implementation repository.
`uart-ip` is the separately versioned UART repository being integrated into it.
`specifications` contains normative interface documents for both projects.
PROJECT_CONTEXT

team ./teams/jon-rtl ro
team ./teams/rtl-auditor ro

mount core-et /home/user/openhw/core-et rw
mount uart-ip /home/user/openhw/uart-ip rw
mount specifications ./references/specifications ro
```

The syntax is whitespace-delimited and has no quoting or `~` expansion.
Relative paths resolve beside the file.

The optional literal `context <<MARKER` block explains what the mounted
projects are, where work belongs, and how the repositories and supporting
inputs relate. On launch or refresh, Cyclo creates the fixed, read-only
`/agentws/project.cyclo` snapshot. It uses the same grammar, keeps the authored
name, description, and context, selects the current team as `/team`, and
rewrites structured paths automatically: `rw` mounts become `/workspace/NAME`
and `ro` mounts become `/readonly/NAME`. Authored description/context text is
copied literally. Every team agent reads that
instance-wide contract; tasks contain only task-specific work.

- `team PATH ro` mounts the team definition read-only at `/team`.
- `team PATH rw` deliberately permits team self-modification.
- `mount NAME PATH rw` creates one writable project at `/workspace/NAME`.
- `mount NAME PATH ro` creates `/readonly/NAME`.

Several `rw` lines intentionally expose several writable projects. Cyclo does
not infer which is primary; the description and context explain their
relationship. Read-only mounts are supporting inputs, not projects.

Validate before running:

```sh
cyclo validate ./project.cyclo
```

## Run and operate teams

Start one container per team line:

```sh
cyclo run ./project.cyclo
```

Useful options:

```text
--offline           remove direct network access; provider UDS remains usable
--host ADDRESS      AgentWS viewer bind address (default 127.0.0.1)
--port PORT         fixed viewer port for a one-team project; 0 chooses one
--verbose           verbose team runtime logs
--foreground        attach for a one-team project
--dry-run           print the container launch without starting it
```

Inspect instances:

```sh
cyclo ps
cyclo inspect INSTANCE
cyclo logs INSTANCE
cyclo logs -f INSTANCE
cyclo path INSTANCE
```

Submit a Markdown task specification:

```sh
cyclo task run INSTANCE uart-ip ./uart-task.md
cyclo task list INSTANCE
cyclo task show INSTANCE uart-ip
```

`task` is an instance-scoped command group:

```text
cyclo task list INSTANCE
cyclo task show INSTANCE TASK
cyclo task run INSTANCE TASK SPEC.md
cyclo task comment INSTANCE TASK MESSAGE...
cyclo task complete INSTANCE TASK [-m MESSAGE]
cyclo task reopen INSTANCE TASK [-m MESSAGE]
```

`task run` atomically creates the AgentWS task and its initial planner job, then
prints the project definition and logical writable/read-only mounts seen by the
team. The other commands expose the task lifecycle without requiring operators
to know the container name or invoke AgentWS directly.

Stop one instance or every persisted instance belonging to a project:

```sh
cyclo stop INSTANCE
cyclo stop ./project.cyclo
```

Queue state and transcripts survive the team-container removal.

When a stopped instance binding is no longer wanted, retire it explicitly:

```sh
cyclo forget INSTANCE --confirm INSTANCE
```

This permanently removes that instance's tasks, jobs, transcripts, generated
runtime, and metadata. It requires the instance's recorded intent to be
`stopped`; use `cyclo stop` first. The explicit operation allows a later project
at a moved path to reuse the logical instance name without silently mixing old
queue state into the new project. Cyclo records `deleting`, cleans the exact
container and network, and moves the state out of ordinary inventory before
purging it. If interrupted, `cyclo repair` finishes that deletion. Repeating
the same exactly confirmed command after deletion has completed succeeds as an
already-absent operation.

## Dashboards

Start the read-only fleet dashboard:

```sh
cyclo dashboard
cyclo dashboard --host 0.0.0.0 --port 8080
```

The default is loopback. Version 0.2.0 has no dashboard authentication; protect
any non-loopback binding with host networking or a trusted reverse proxy. The
dashboard derives AgentWS links from the browser's current host and the
instance port; `0.0.0.0` is a bind address, never a link target.

Each team also runs its read-only AgentWS viewer. The interactive chat surface
is not part of Cyclo's job model.

## Usage and health

Show gateway accounting:

```sh
cyclo usage
```

Usage is global by account/provider and exact model. The shared socket has no
trustworthy team identity, so Cyclo does not invent per-team attribution.
Observation never creates a missing gateway credential store.

Run the non-mutating installation check:

```sh
cyclo doctor
```

It verifies the bundled AgentWS and component ABI, persisted state, Docker,
`host.conf`, exact image/container state, each component's own health, provider
bindings, and the selected model catalogue.

`cyclo ps` distinguishes team-container state from provider readiness. A team
can be running while its model path is unavailable; the health reason makes
that explicit.

After an interrupted team start or stop, reconcile launch-pinned metadata and
clean only stale owned resources with:

```sh
cyclo repair
```

Repair makes the recorded lifecycle intent true. It recreates missing
`running` instances from their current `project.cyclo`, replaces paused or
restarting instances, verifies AgentWS readiness for every running container
before saving a recovered published port, removes Docker resources for
`stopped` instances, and completes `deleting` instances. It reports every
remaining failure and exits nonzero if any operation was incomplete.

## Persistent state

The state root is `$XDG_STATE_HOME/cyclo` or `~/.local/state/cyclo` by default.
Select it explicitly with `--state-root` or `CYCLO_STATE_ROOT`. When the
provider graph is first applied, the implicit root uses `/etc/cyclo/host.conf`;
an explicitly selected root uses `host.conf` inside that root. Cyclo persists
this association in the state root, so later environment changes cannot switch
the provider configuration for an existing installation. Read-only checks do
not create the binding, and gateway-only commands do not depend on it.

### Multiple installations on one host

An installation is identified by its canonical state root. Cyclo derives a
stable 12-hex-character installation ID from the root's fixed `components/`
path and uses it in every Docker resource it creates: the gateway and provider
containers/images, gateway credential volume, team containers and networks,
common and derived team images, and ownership labels. Two installations may
therefore use the same project name, team name, and instance ID without Docker
name or mutable-tag collisions.

Give each installation its own state root and put its provider configuration
there:

```sh
mkdir -p ~/.local/state/cyclo-work ~/.local/state/cyclo-lab
printf '%s\n' 'provider passthrough /opt/providers/passthrough upstream=gateway' > ~/.local/state/cyclo-work/host.conf
CYCLO_STATE_ROOT=~/.local/state/cyclo-work cyclo gateway providers
CYCLO_STATE_ROOT=~/.local/state/cyclo-lab cyclo gateway providers
```

Use the same state-root setting on every command for that installation. Shell
wrappers or environment files are convenient, but Cyclo needs no second binary
installation. The equivalent explicit form is:

```sh
cyclo --state-root ~/.local/state/cyclo-work ps
```

The state root owns the gateway credential store, usage data, queues, sockets,
Docker namespace, and optional provider configuration. `cyclo gateway status`,
`cyclo providers status`, `cyclo ps`, and `cyclo doctor` inspect only the
selected installation.

`--image IMAGE` and `CYCLO_TEAM_IMAGE` deliberately bypass the namespaced
common/derived image selection. Cyclo validates but does not build that
operator-supplied image. Use the override only when one externally managed
image is intended for every team in the project.

As established at the start of this guide, 0.2 does not adopt or migrate 0.1
state, containers, networks, images, or provider configuration. Recreate
projects from their `project.cyclo` files. The separately selected state root
keeps an old installation isolated if it must remain available during the
transition.

```text
instances/INSTANCE/
  runtime/          materialized read-only AgentWS runtime
  agentws-state/
    tasks/ jobs/ agents/
  pi/               writable Pi settings and runtime metadata
  workspace/        inert named writable layout
  readonly/         inert named read-only layout
components/gateway/socket/       root gateway socket
components/sockets/COMPONENT/    intermediate component sockets
```

The gateway credential and usage store is a separately labelled Docker volume.
Cyclo verifies installation, resource-kind, instance, and persisted launch
identity before operating on a current team container. Startup replacement
uses the first three labels so it can remove a stopped previous launch; it
never adopts that previous launch as the current instance.

## Security model

Cyclo's trusted-host threat model, resource-capability semantics, and extension
points are defined in [Security architecture](architecture.md#security-architecture).
In particular, the host is the administrative security domain while agent code
inside a team container is treated as potentially hostile.

- Only the gateway mounts physical credentials.
- Teams and intermediate components receive no Docker socket.
- Provider edges are explicit read-only Unix-socket directory mounts.
- Intermediate providers use `--network none`.
- Teams see only declared team/project paths and the outer provider socket.
- Incoming RPC authentication headers are not forwarded through components.
- Gateway-owned API keys, native model routes, headers, environment, callbacks,
  abort signals, transports, timeouts, and retry controls cannot be selected in
  inference JSON.
- Prompt and tool data are intentionally visible to an allowed model. Cyclo is
  not a confidentiality boundary against a provider the operator chose.

## Maintainer checks

From the source tree:

```sh
pytest -q
npm --prefix src/cyclo/components/protocol/component test
npm --prefix src/cyclo/components/protocol/provider test
npm --prefix src/cyclo/components/gateway test
npm --prefix src/cyclo/components/passthrough test
npm --prefix src/cyclo/components/pi-provider test
tools/build-release
tools/release-acceptance
```

`tools/release-acceptance` builds and exercises an isolated wheel.
`tools/build-release` additionally builds the real gateway, pass-through, and
team images. The source tests exercise real ConnectRPC Unix sockets across the
gateway endpoint, relay, and team-side Pi adapter without external credentials.
