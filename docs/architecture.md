# Cyclo architecture

Cyclo separates four things that often get mixed together in agent runners:
the team definition, the project being changed, the filesystem work queue, and
provider credentials. Each has an independent owner and lifecycle.

## Components

```text
team Git repository                 project Git repository
team + roles/*.md                   source and tests
        | read-only by default             | writable by default
        +-------------------+---------------+
                            v
                 cyclo-runtime container
             bundled queue + agent processes
                            |
                 scoped private capability
                            |
                            v
                 cyclo-gateway container
            model catalogue + policy + proxy
                            |
                 cyclo-gateway-store volume
       credentials + subscriptions + usage ledger
```

The host-side `cyclo` command validates definitions and mount boundaries,
builds packaged Docker contexts, creates isolated instance state, reconciles
Docker resources, and issues a capability scoped to the providers and models
declared by that team generation.

The host controller is Python. The reused Pi agent engine and `pi-ai` provider
and subscription support are JavaScript, but they are installed and executed
inside the runtime and gateway images. Running Cyclo therefore does not require
Node.js or npm on the host; those tools are host prerequisites only for the
complete maintainer test and release workflow.

The runtime image contains Cyclo's owned filesystem loop, Pi agent engines, and
supporting command-line tools. It receives the team, project, and per-instance
queue state as explicit mounts. It never receives the Docker socket, gateway
administrator token, credential store, host home directory, or another team's
state.

The gateway image is the only component that mounts `cyclo-gateway-store`.
Interactive subscription logins and API-key provisioning run inside that
image. The long-running proxy projects only allowed models to each runtime and
records provider-reported token accounting by instance and team generation.

## Definitions and generations

A team is an ordinary Git repository containing a `team` roster and role files.
The roster binds every named agent to a role, engine, and proxy model. Cyclo
identifies a generation with the repository commit plus a digest of the live
roster, roles, and optional protocol, so experiments remain attributable even
when the team has uncommitted edits.

The default team mount is read-only. `--team-write` deliberately permits a team
to modify its own definition; those changes are ordinary Git working-tree
changes and apply on the next run. Project writability is controlled
independently with `--project-read-only`.

## Queue and controller state

Each instance has durable host state below the selected Cyclo state root. The
runtime sees a materialized, read-only copy of the bundled queue implementation
and a writable instance queue. Tasks, jobs, comments, transcripts, and results
survive container replacement. Atomic publication, locking, bounded retries,
and interrupted-write recovery are implemented by the bundled loop.

Controller state contains paths, lifecycle metadata, scoped client records,
and a writable per-instance Pi tree containing projected model configuration,
the scoped gateway token, locks, and local runtime metadata. It does not contain
host or provider credentials; those remain in the gateway store. Stopping an
instance removes its container and private network, revokes its capability, and
preserves its queue history.

## Networks and model traffic

Each instance receives a private Docker network. In normal mode the runtime can
also use direct outbound networking. `--offline` makes its network internal;
the gateway is attached to that network, so allowed model calls still work but
direct web egress and the per-team viewer do not.

The scoped gateway capability is provider-and-model authorization plus usage
attribution. It is not a confidentiality boundary between the mounted project
and an allowed model provider: an agent can send readable project content in a
model request. Use read-only mounts and offline mode to reduce privileges, and
use separate Git worktrees for concurrent writers.

## Persistent gateway data

`cyclo-gateway-store` contains credentials, subscription sessions, and an
append-only JSONL usage ledger. Usage records contain accounting and
attribution metadata, not prompts or responses. `cyclo usage` aggregates the
retained ledger for experiments. Version 0.1.0 does not impose automatic
retention; operators should monitor the Docker volume.

`cyclo gateway destroy-store` is intentionally fail-closed and confirmation
gated. It deletes the entire volume, including credentials, subscriptions, and
usage history, only after verifying every mounting Cyclo container by immutable
identity. A foreign, unverifiable, racing, or still-running mount causes refusal
or lets Docker's final in-use check preserve the volume.

## Observation boundary

The fleet dashboard and per-team viewer are read-only. Both bind to loopback by
default and have no application authentication in 0.1.0. An operator can
explicitly select a non-loopback host for either interface, in which case Cyclo
prints an exposure warning. Queue scans are bounded, queue content is treated
as data, and the dashboard never starts the gateway or executes queue files. A
team cannot read another team's private queue through Cyclo; cross-team
supervision requires a future explicit, read-only observation interface.

## Docker-host trust

Cyclo treats team containers and writable Git trees as untrusted, but trusts
the host kernel, Docker daemon, Cyclo installation, gateway image, and
administrator account. Compromise of the Docker host or access to the Docker
socket is outside the isolation guarantee. The detailed reporting and support
policy is in [`../SECURITY.md`](../SECURITY.md).
