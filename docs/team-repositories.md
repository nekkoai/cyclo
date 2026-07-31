# Team repository contract

A Cyclo team is a Git repository containing agent behavior and optional
execution-image additions. It is not a standalone daemon and does not implement
the DComp or Provider protocols.

At runtime Cyclo wraps the repository in its common team image and emits one
DComp team component per project selection. The component consumes one
`cyclo.provider.v1.Provider` input and runs the bundled AgentWS supervisor.

## Repository contents

The minimal repository is:

```text
my-team/
  team
  roles/
    planner.md
    builder.md
    verifier.md
```

Optional files are:

```text
  AGENTS.md
  Dockerfile
  .dockerignore
  README.md
  LICENSE
```

Cyclo validates the `team` roster, every Markdown file directly under `roles/`,
optional `AGENTS.md`, and optional Dockerfile contract. Other files are not part
of the team definition, although agents can see them through `/team`.

The selected directory must be the root of a Git repository. `team` is the
canonical roster name; `default.team` remains accepted for compatibility.

## Roster

Each non-comment roster line has four fields:

```text
AGENT_NAME ROLE ENGINE PROVIDER/MODEL
```

Example:

```text
planner-1   planner   pi   codex-work/MODEL_ID
builder-1   builder   pi   codex-work/MODEL_ID
verifier-1  verifier  pi   claude-work/MODEL_ID
```

Rules:

- agent names are unique;
- each role has a matching `roles/ROLE.md`;
- at least one agent has the `planner` role;
- engines are `pi` or `pi-interactive`; and
- the model is an exact public ID advertised by `cyclo models`.

The roster selects model routes. It is not a separate authorization token or
credential boundary.

## Instruction layers

Every agent receives these instruction sources:

1. Cyclo's generic AgentWS `AGENTS.md`, baked into the team image;
2. `/agentws/project.cyclo`, generated from the selected project;
3. optional team-wide `/team/AGENTS.md`;
4. the selected `/team/roles/ROLE.md`; and
5. the assigned AgentWS task and job.

The generic protocol defines queue behavior, planner notifications, project
discovery, and failure settlement. A team `AGENTS.md` may specialize behavior
but does not replace those invariants. A role file describes only that role.

Agents must read `/agentws/project.cyclo` before choosing a source directory.
It tells them which paths below `/workspace` are writable projects and which
paths below `/readonly` are supporting inputs.

## Runtime filesystem

A team component sees:

```text
/agentws/
  AGENTS.md              baked generic protocol
  bin/ tools/ roles/     baked AgentWS runtime
  tasks/                 durable writable bind
  jobs/                  durable writable bind
  agents/                durable writable bind
  project.cyclo          generated read-only bind
/opt/cyclo/pi-settings.json generated read-only Pi settings template
/home/cyclo/.pi          private writable Pi state
/team                    selected team repository
/workspace/NAME          each project `rw` mount
/readonly/NAME           each project `ro` mount
```

The team repository is mounted read-only by default. A project may select it
with `team PATH rw` to authorize self-modification. That is a deliberate
authority grant: changes are live in the mounted repository, while Cyclo's
recorded team generation is updated only by a later `cyclo refresh`.

The Provider adapter obtains its endpoint from the DComp input:

```text
DCOMP_LINK_PROVIDER=dns:///OUTER_COMPONENT:50051
```

Agents and role files should use Pi normally; they do not need to handle this
environment variable or know the provider topology.

Team containers receive no gateway credential volume, Docker socket, DComp
state, host Pi configuration, or undeclared filesystem mount.

## Durable AgentWS state

AgentWS tasks, jobs, comments, results, retry state, and agent transcripts live
under the Cyclo state root, not in the team repository or container writable
layer. Replacing or stopping a DComp team component therefore does not discard
work.

Task operations are available from the host:

```sh
cyclo task run INSTANCE TASK_ID SPEC_FILE
cyclo task list INSTANCE
cyclo task show INSTANCE TASK_ID
cyclo task comment INSTANCE TASK_ID MESSAGE
cyclo task complete INSTANCE TASK_ID
cyclo task reopen INSTANCE TASK_ID
```

At component startup the supervisor resets orphaned active jobs, starts the
read-only AgentWS viewer and queue runner, and holds a queue lifetime lock.
Shutdown terminates both children with a bounded grace period.

## Team generation

Cyclo records the repository's current Git commit plus a digest of the roster,
role files, and optional `AGENTS.md`. Uncommitted definition changes are
therefore part of the generation even though they are not a Git commit.

Cyclo reads definition files without following symlinks and bounds their size.
It does not run `git status` because repository-local Git hooks and filesystem
monitor configuration are host execution surfaces.

Changing a team does not silently replace a running component. Use:

```sh
cyclo refresh
```

Refresh reparses the project and team, validates current models and mounts,
rebuilds the team image when required, updates the persisted generation, and
applies the global DComp system.

## Extra packages with a Dockerfile

Cyclo's common team image already contains AgentWS, Pi, the Provider adapter,
Git, Python, Node.js, and common shell tools. A team that needs more packages
adds a Dockerfile:

```dockerfile
ARG CYCLO_TEAM_BASE
FROM ${CYCLO_TEAM_BASE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends verilator yosys \
    && rm -rf /var/lib/apt/lists/*
```

The contract is strict:

- `ARG CYCLO_TEAM_BASE` appears before the relevant `FROM`;
- the final stage uses `FROM ${CYCLO_TEAM_BASE}` or
  `FROM $CYCLO_TEAM_BASE`;
- the final image preserves
  `/usr/local/bin/cyclo-container-entrypoint`;
- the final image preserves the inherited OCI health check; and
- the final image's configured user is root, `0`, or empty.

The root image user is required only so Cyclo's entrypoint can select the
host-mapped `cyclo` UID/GID. It immediately drops privileges before executing
the AgentWS runtime. Cyclo refuses team operations when the invoking host UID is
zero.

Cyclo gives the derived image a stable tag scoped by the installation, Cyclo
version, team name, and canonical team-repository identity. It invokes
`docker build` with the repository as the real context and passes the common
team tag as `CYCLO_TEAM_BASE`. Docker applies `.dockerignore` and decides
layer-cache reuse. Cyclo validates the completed tag and base-image identity,
then persists only the immutable image ID in the instance. It keeps no
source-digest cache or build history.

Use a `.dockerignore` to keep `.git`, generated output, caches, and local
artifacts out of the build context.

## Build trust

Running a team Dockerfile authorizes Docker to execute that repository's build
steps in the trusted host domain. Review it as installed software. Runtime
mount/network restrictions do not make a hostile Docker build safe.

An operator may bypass team Dockerfiles with:

```sh
cyclo run project.cyclo --image OPERATOR_IMAGE
```

Cyclo requires that image to exist and satisfy the same entrypoint, user, and
health-check contract. It does not build or modify it.

## Reusable toolchain bases

An organization can extend the common runtime once, then use that approved
image as `CYCLO_TEAM_BASE` for specialized teams:

```text
Cyclo common team image
        |
        v
organization RTL toolchain
        |
        v
specific RTL team
```

This is ordinary single-parent Docker inheritance. Multi-stage builds may
compile artifacts, but they do not merge package state from multiple final
parents. Cyclo intentionally does not add a second package-description
language.
