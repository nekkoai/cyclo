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

Install from a source checkout or release artifact using the Python environment
policy appropriate for the machine:

```sh
python3 -m pip install .
cyclo --version
cyclo doctor
```

The distribution is named `cyclo-agent`; the command and Python package are
named `cyclo`.

## First gateway

Build and start the isolated credential gateway:

```sh
cyclo gateway build
cyclo gateway status
```

`gateway build` promotes the successfully built image and restarts the gateway
on it. A separate `gateway start` is unnecessary. The private credential volume
is created if absent and survives ordinary build, stop, and restart operations.

List login choices before storing any credential:

```sh
cyclo gateway providers
```

Login using OAuth/subscription or an API key:

```sh
cyclo gateway login openai-codex --as codex-work
cyclo gateway login anthropic --as claude-work
cyclo gateway login openai --as openai-work --api-key-stdin
cyclo gateway login openai --as openai-work --api-key-env OPENAI_API_KEY
```

The account name selected by `--as` becomes the public prefix in
`ACCOUNT/MODEL`. Login changes the store; restart publishes the resulting model
catalogue:

```sh
cyclo gateway restart
cyclo models
```

The gateway commands are:

```text
cyclo gateway providers
cyclo gateway login PROVIDER [--as ACCOUNT] [--api-key-stdin|--api-key-env NAME]
cyclo gateway build
cyclo gateway start
cyclo gateway restart [--build]
cyclo gateway stop
cyclo gateway status
cyclo gateway destroy-store --confirm VOLUME
```

`destroy-store` is the explicit destructive operation for credentials and
usage. `cyclo gateway status` prints `VOLUME`; no other gateway command deletes
it.

## Provider components

The gateway is the fixed root Provider. Optional intermediate providers are an
ordered component graph declared by `/etc/cyclo/host.conf` (or `--host-config`):

```text
# provider INSTANCE SOURCE [context=PATH] REQUIREMENT=TARGET ... [-- ARGUMENT ...]
provider trace ./providers/passthrough upstream=gateway -- label=first
provider outer ./providers/passthrough upstream=trace
```

Each `SOURCE` directory contains:

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
after `--` are passed separately to the component entrypoint. A `context=PATH`
setting selects a Docker build context containing the component source.

An absent or empty `host.conf` is valid and exposes the gateway directly.
Relative source paths resolve from the configuration file's directory.

Operate the complete configured stack with:

```sh
cyclo providers check
cyclo providers build
cyclo providers start
cyclo providers restart
cyclo providers restart --build
cyclo providers status
cyclo providers stop
```

`start` never builds. `restart` rebuilds only with explicit `--build`.
`stop` removes all provider-lifecycle containers owned by this Cyclo state root
even if the current configuration is temporarily invalid. The gateway remains
independent.

Every provider component gets only its own output socket directory and the
read-only socket directories named by its requirements. Intermediate
components run with no network. The final component socket—or the gateway
socket for an empty stack—is mounted read-only into teams.

The Provider data plane carries `model` and an opaque Pi JSON string. Relays do
not understand prompts, history, tools, JSON Schema, or events. See
[Provider protocol v1](provider-protocol.md).

## Create a team

List installed templates:

```sh
cyclo team templates
```

Create a team repository using an exact model from `cyclo models`:

```sh
cyclo team init ./teams/my-team --template plan-execute-verify --model codex-work/MODEL_ID
```

The repository contains:

```text
team
roles/
  planner.md
  ...
AGENTS.md          # optional team-wide additions
```

The roster format is:

```text
AGENT ROLE ENGINE PROVIDER/MODEL
```

Supported engines are `pi` and `pi-interactive`. Each role needs a matching
`roles/ROLE.md`. At least one agent must have role `planner` because new tasks
begin with a planner job.

Cyclo supplies AgentWS, Pi, and the provider extension. The team repository is
data: roster, prompts, and optional instructions. Validate it with:

```sh
cyclo validate ./teams/my-team
```

After upgrading Cyclo or changing installed component source, rebuild the
gateway, provider components, and team runtime and restart every active
`project.cyclo` run with one command:

```sh
cyclo refresh
```

Queue state is preserved. Every active instance must retain its `project.cyclo`
path so Cyclo can reproducibly recreate the same mount and team authority.

## Define a project

Create a validated one-team project definition directly:

```sh
cyclo project init ./project.cyclo --team ./teams/jon-rtl ro --mount source /home/user/openhw/core-et rw
```

Add further `--team PATH MODE` or `--mount NAME PATH MODE` options as needed.
Alternatively, write the same format by hand:

Create `project.cyclo`:

```text
name core-et-uart
description Design and verify a UART IP.

team ./teams/jon-rtl ro
team ./teams/rtl-auditor ro

mount source /home/user/openhw/core-et rw
mount specifications ./references/specifications ro
```

The syntax is whitespace-delimited and has no quoting or `~` expansion.
Relative paths resolve beside the file.

- `team PATH ro` mounts the team definition read-only at `/team`.
- `team PATH rw` deliberately permits team self-modification.
- `mount NAME PATH rw` creates `/workspace/NAME`.
- `mount NAME PATH ro` creates `/readonly/NAME`.

Read-only mounts are supporting inputs, not projects. Writable projects always
live below `/workspace`.

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
--build             rebuild the bundled team image
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

Run the non-mutating installation check:

```sh
cyclo doctor
```

It verifies the bundled AgentWS and component ABI, persisted state, Docker,
`host.conf`, exact image/container state, component health, dependency health,
and the outer model catalogue.

`cyclo ps` distinguishes team-container state from provider readiness. A team
can be running while its model path is unavailable; the health reason makes
that explicit.

After an interrupted team stop, repair only stale owned resources with:

```sh
cyclo repair
```

## Persistent state

The state root is `$XDG_STATE_HOME/cyclo` or `~/.local/state/cyclo` by default.
Override it with `--state-root` or `CYCLO_STATE_ROOT`.

### Multiple installations on one host

An installation is identified by its canonical state-root path. Cyclo derives
a stable 12-hex-character installation ID from that path and uses it in every
Docker resource it creates: the gateway and provider containers/images, gateway
credential volume, team containers and networks, the bundled team image, and
ownership labels. Two installations may therefore use the same project name,
team name, and instance ID without Docker name or mutable-tag collisions.

Give each installation both its own state root and its own provider assembly:

```sh
CYCLO_STATE_ROOT=~/.local/state/cyclo-work CYCLO_HOST_CONFIG=/etc/cyclo/work.conf cyclo gateway build
CYCLO_STATE_ROOT=~/.local/state/cyclo-lab CYCLO_HOST_CONFIG=/etc/cyclo/lab.conf cyclo gateway build
```

Use those same two settings on every command for that installation. Shell
wrappers or environment files are convenient, but Cyclo needs no second binary
installation. The equivalent explicit form is:

```sh
cyclo --state-root ~/.local/state/cyclo-work --host-config /etc/cyclo/work.conf ps
```

The state root owns the gateway credential store, usage data, queues, sockets,
and Docker namespace. The host configuration owns only the optional provider
assembly; do not point two supposedly independent installations at the same
state root. `cyclo gateway status`, `cyclo providers status`, `cyclo ps`, and
`cyclo doctor` inspect only the selected installation.

`--image IMAGE` and `CYCLO_TEAM_IMAGE` deliberately override the namespaced
default team image. Use that override only when sharing one image between
installations is intended.

Cyclo 0.2 is a fresh-install boundary. It does not adopt or migrate 0.1 state,
containers, networks, images, or provider-runtime configuration. Use a new
state root, build the 0.2 resources, and recreate projects from their
`project.cyclo` files. A different state root keeps an old installation
separate if it must remain available during the transition.

```text
instances/INSTANCE/
  runtime/          materialized read-only AgentWS runtime
  agentws-state/
    tasks/ jobs/ agents/
  pi/               writable Pi settings and runtime metadata
  workspace/        inert named writable layout
  readonly/         inert named read-only layout
gateway/socket/             root gateway socket
sockets/COMPONENT/          intermediate component sockets
```

The gateway credential and usage store is a separately labelled Docker volume.
Cyclo verifies installation, resource-kind, and instance ownership before
adopting, mounting, or deleting Docker resources.

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
tools/release-acceptance dist
```

Release acceptance additionally builds the real gateway, pass-through, and
team images. The source tests exercise real ConnectRPC Unix sockets across the
gateway endpoint, relay, and team-side Pi adapter without external credentials.
