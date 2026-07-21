<p align="center"><img src="docs/assets/cyclo-logo.svg" alt="Cyclo" width="176"></p>

<h1 align="center">Cyclo</h1>

<p align="center"><strong>Agentic systems, in a Git loop.</strong></p>

Cyclo runs repository-defined agent teams against real projects. Credentials
stay in an isolated gateway; teams see only a provider socket and the mounts
declared by their project. Everything needed to run the system is shipped in
this repository—there are no `agentws` or `multiagent` checkouts at runtime.

## Quick start

Requirements: Linux, Python 3.10+, Git, and Docker.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cyclo gateway build
cyclo gateway start
cyclo gateway providers
cyclo gateway login openai-codex --as codex-work
cyclo providers start --all
cyclo models
cyclo doctor
```

`gateway providers` is useful before login: it lists providers and explains
which login method each accepts. The gateway stores OAuth sessions and API
keys in its Docker-managed state volume; they are never mounted into teams.

## Host providers

`/etc/cyclo/host.conf` is an ordered, line-oriented configuration:

```text
provider fusion ./providers/fusion codex-work/gpt-5 mode=balanced
```

The first field is the output prefix, the second is a provider directory (or
component reference), following fields are input model IDs, and `key=value`
fields are provider parameters. Inputs may refer to gateway models or models
produced by an earlier line. Relative paths resolve beside `host.conf`. An
empty file means “use the gateway catalogue unchanged”. Edit the file, then
run `cyclo providers restart`; Cyclo does not rebuild images implicitly.

Providers are independent components using the ConnectRPC provider protocol
over Unix-domain sockets. The gateway is the fixed root provider and catalogue
authority. `cyclo models` prints the composed catalogue and `cyclo doctor`
checks gateway, provider components, Docker image identity, and health.

## Projects and teams

A project directory contains a `project.cyclo` file and one or more team
repositories:

```text
name: rtl-work
description: RTL design experiment
team jon-rtl ./teams/jon-rtl
mount source ~/openhw/core-et rw
mount docs ./docs ro
```

`rw` mounts are workspaces at `/workspace/<name>`; `ro` mounts are supporting
inputs at `/readonly/<name>`. Teams cannot write read-only mounts. A team
repository contains `roles.md`, its agent roster, `AGENTS.md`, and optional
Docker build additions. Cyclo supplies the common AgentWS job loop and the
provider socket; the team supplies data and prompts.

Run and inspect work:

```sh
cyclo run project.cyclo
cyclo task project.cyclo "Implement and verify a UART IP"
cyclo ps
cyclo logs <instance>
cyclo dashboard --bind 127.0.0.1
```

The dashboard is read-only and shows team/job state, provider health, and
global provider usage. Use `--bind 0.0.0.0` only when exposing it deliberately;
the browser link uses the host that served the page, not the bind wildcard.

## Documentation

- [Architecture](docs/architecture.md)
- [Project format](docs/project-format.md)
- [Provider protocol](docs/provider-protocol.md)
- [Operations guide](docs/guide.md)
- [Release checklist](docs/releasing.md)

The source tree is self-contained under `src/cyclo/_bundle`. The Python CLI is
the public interface; Node is used inside component images and by maintainer
tests, not as a host installation requirement.
