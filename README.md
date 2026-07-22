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
python -m pip install .
cyclo gateway build
cyclo gateway providers
cyclo gateway login openai-codex --as codex-work
cyclo gateway restart
cyclo providers check
cyclo providers restart --build
cyclo models
cyclo doctor
```

`gateway providers` is useful before login: it lists providers and explains
which login method each accepts. The gateway stores OAuth sessions and API
keys in its Docker-managed state volume; they are never mounted into teams.

## Host providers

`/etc/cyclo/host.conf` is an ordered, line-oriented configuration:

```text
provider trace ./providers/passthrough upstream=gateway -- label=first
provider outer ./providers/passthrough upstream=trace
```

The first field is a host-local component instance and the second is its source
directory. Named requirements from that repository's `component.conf` bind to
`gateway` or an earlier instance. Words after `--` become separate component
arguments. Relative paths resolve beside `host.conf`. An empty file means “use
the gateway directly”. Edit the file, then run `cyclo providers restart`;
Cyclo rebuilds only when `--build` is explicit.

Providers are independent components using the ConnectRPC provider protocol
over Unix-domain sockets. The gateway is the fixed root provider and catalogue
authority. `cyclo models` prints the composed catalogue and `cyclo doctor`
checks gateway, provider components, Docker image identity, and health.

## Projects and teams

A project directory contains a `project.cyclo` file and one or more team
repositories:

```text
name rtl-work
description RTL design experiment
team ./teams/jon-rtl ro
mount source /home/user/openhw/core-et rw
mount docs ./docs ro
```

`rw` mounts are workspaces at `/workspace/<name>`; `ro` mounts are supporting
inputs at `/readonly/<name>`. Teams cannot write read-only mounts. A team
repository contains a `team` roster, `roles/*.md`, and optional `AGENTS.md`.
Cyclo supplies the common AgentWS job loop and the provider socket; the team
supplies data and prompts.

Run and inspect work:

```sh
cyclo run project.cyclo
cyclo ps
cyclo task <instance> uart-ip ./uart-task.md
cyclo logs <instance>
cyclo dashboard --host 127.0.0.1
```

The dashboard is read-only and shows team/job state, provider health, and
global provider usage. Use `--host 0.0.0.0` only when exposing it deliberately;
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
