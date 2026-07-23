<p align="center"><img src="docs/assets/cyclo-logo.svg" alt="Cyclo" width="176"></p>

<h1 align="center">Cyclo</h1>

<p align="center"><strong>Agentic systems, in a Git loop.</strong></p>

Cyclo runs repository-defined agent teams against real projects. Credentials
stay in an isolated gateway; teams see only a provider socket and the mounts
declared by their project. Everything needed to run the system is shipped in
this repository—there are no `agentws` or `multiagent` checkouts at runtime.

## Quick start

Requirements: Linux, Python 3.10+, Git, and Docker.

Cyclo 0.2 uses a new persisted-state and Docker-resource layout. It does not
perform an in-place migration from 0.1: install it with a fresh state root and
newly built gateway, provider, and team resources.

```sh
python -m pip install .
cyclo gateway providers
cyclo gateway login openai-codex --as codex-work
cyclo models
cyclo doctor
```

`gateway providers` is useful before login: it lists providers and explains
which login method each accepts. The gateway stores OAuth sessions and API
keys in its Docker-managed state volume; they are never mounted into teams.

## Host providers

`/etc/cyclo/host.conf` is an ordered, line-oriented configuration:

```text
provider first ./providers/passthrough upstream=gateway
provider second ./providers/passthrough upstream=first
```

The first field is a host-local component instance and the second is its source
directory. Named requirements from that repository's `component.conf` bind to
`gateway` or an earlier instance. Words after `--` become separate component
arguments that explicitly replace the image's OCI `CMD`; without `--`, the
image's own `CMD` is used unchanged. Relative paths resolve beside `host.conf`.
An empty file means “use the gateway directly”. Mutating lifecycle commands
submit build contexts to Docker, validate completed images, and replace stale
containers automatically. Docker owns `.dockerignore` and cache semantics;
`status` and `doctor` remain observational.

Providers are independent components using the ConnectRPC provider protocol
over Unix-domain sockets. The gateway is the fixed root provider and catalogue
authority. `cyclo models` prints the composed catalogue and `cyclo doctor`
checks gateway, provider components, Docker image identity, and health.

Every component can be inspected on its own:

```sh
cyclo component list
cyclo component status gateway
cyclo component status first
cyclo component logs first
```

Each row is factual: image/container state, whether the launch configuration is
current, Docker health, Component health, and any concrete error. There is no
separate registry or graph-wide readiness flag. If an intermediate provider
fails, Cyclo reports that component and selects the last working provider, so a
broken optional component does not hide gateway models.

## Projects and teams

A project directory contains a `project.cyclo` file and one or more team
repositories:

```sh
cyclo team init ./teams/jon-rtl --template plan-execute-verify --model PROVIDER/MODEL
cyclo project init ./project.cyclo --team ./teams/jon-rtl ro --mount source /home/user/openhw/core-et rw
```

The generated project file uses the same small line-oriented format:

```text
name rtl-work
description RTL design experiment
team ./teams/jon-rtl ro
mount source /home/user/openhw/core-et rw
mount docs ./docs ro
```

`rw` mounts are workspaces at `/workspace/<name>`; `ro` mounts are supporting
inputs at `/readonly/<name>`. Teams cannot write read-only mounts. A team
repository contains a `team` roster, `roles/*.md`, optional `AGENTS.md`, and an
optional Dockerfile derived from `CYCLO_TEAM_BASE` when its agents need extra
tools. Cyclo builds and selects that image per team. Cyclo supplies the common
AgentWS job loop and provider socket; the team supplies behavior and its
additional execution dependencies.

Run and inspect work:

```sh
cyclo run project.cyclo
cyclo ps
cyclo inspect <instance>
cyclo task run <instance> uart-ip ./uart-task.md
cyclo task list <instance>
cyclo task show <instance> uart-ip
cyclo logs <instance>
cyclo dashboard --host 127.0.0.1
cyclo refresh                       # rebuild runtimes and restart active projects
```

The dashboard is read-only and shows team/job state, provider health, and
global provider usage. Use `--host 0.0.0.0` only when exposing it deliberately;
the browser link uses the host that served the page, not the bind wildcard.

## Multiple installations

One installed `cyclo` command can operate several independent installations on
the same Docker host. The default installation reads `/etc/cyclo/host.conf`.
An explicit state root instead reads `host.conf` from that root:

```sh
mkdir -p ~/.local/state/cyclo-work ~/.local/state/cyclo-lab
printf '%s\n' 'provider passthrough /opt/providers/passthrough upstream=gateway' > ~/.local/state/cyclo-work/host.conf
CYCLO_STATE_ROOT=~/.local/state/cyclo-work cyclo doctor
CYCLO_STATE_ROOT=~/.local/state/cyclo-lab cyclo doctor
```

The canonical state root defines the installation identity. Cyclo includes it
in gateway and provider resources, team containers and networks, the default
team image, and ownership labels, so equal project/team names do not collide.
An explicitly supplied `--image` remains an intentional shared Docker image.

## Documentation

- [Architecture](docs/architecture.md)
- [Project format](docs/project-format.md)
- [Provider protocol](docs/provider-protocol.md)
- [Operations guide](docs/guide.md)
- [Release checklist](docs/releasing.md)

The component system is self-contained under `src/cyclo/components`: shared
ConnectRPC contracts live in `protocol/`, independently runnable components
live beside them, and the agent image is explicit as `team-runtime/`. The
Python CLI is the public interface; Node is used inside component images and by
maintainer tests, not as a host installation requirement.
