# Team repository contract

A Cyclo team is a Git repository that defines agents, not a service. It does
not implement the Component or Provider interfaces, start a daemon, contain
credentials, or own runtime state. Cyclo consumes the repository as data and
runs it with the system-owned team runtime.

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

It may also contain:

```text
  AGENTS.md              # optional team-wide policy
  Dockerfile             # optional execution-image delta
  .dockerignore          # recommended with Dockerfile
  README.md              # human documentation; ignored by Cyclo
  LICENSE                # repository metadata; ignored by Cyclo
```

Cyclo reads `team`, every Markdown file directly below `roles/`, and optional
`AGENTS.md`. Other files are not part of team-definition validation, although
agents can read them because the selected repository is mounted at `/team`.
The canonical roster name for an independent repository is `team`;
`default.team` remains accepted for compatibility.

The roster format is:

```text
# <agent-name> <role> <engine> <provider/model>
planner-1      planner   pi       codex-work/MODEL_ID
builder-1      builder   pi       codex-work/MODEL_ID
verifier-1     verifier  pi       claude-work/MODEL_ID
```

Every role must have a corresponding `roles/<role>.md`, agent names must be
unique, and at least one agent must have the `planner` role. The model is an
exact identifier from the outer Provider catalogue. The roster selects a
model; it is not by itself an authorization boundary.

The intended prompt composition is:

1. the system-owned generic AgentWS protocol;
2. the generated, host-path-free project manifest;
3. optional team-wide policy from `AGENTS.md`; and
4. the selected `roles/<role>.md`.

The system-owned protocol is always included. A repository `AGENTS.md` is
layered after it as team-specific policy, so a team never needs to copy
Cyclo's generic filesystem and job-loop rules merely to add local behavior.
For a writable team, changes to `/team/AGENTS.md` affect subsequently launched
agents without modifying the system protocol.

## Ownership and interaction

Four separate inputs have separate owners:

| Input | Owns |
|---|---|
| Installation `host.conf` | Installation-wide provider composition and available models |
| Team repository | Agent roster, role behavior, optional team policy, and optionally the execution-image delta |
| `project.cyclo` | Which teams run, which directories they receive, and each mount mode |
| Cyclo state root | Tasks, jobs, comments, results, transcripts, Pi state, generated runtime files, and instance metadata |

At runtime, one generic team container receives:

```text
/team                   selected team repository, read-only or read-write
/workspace/<name>       declared writable project repositories
/readonly/<name>        declared read-only supporting inputs
/agentws                Cyclo-supplied AgentWS tools and per-instance queue
$CYCLO_PROVIDER_SOCKET  outer Provider Unix socket
```

Cyclo validates the repository, records its generation, selects its execution
image, and launches the agents in the roster. Agents coordinate through the
AgentWS filesystem tools and reach models through the Provider interface. The
repository itself calls no Cyclo API and executes no service.

A `team PATH ro` line is the normal reproducible mode. `team PATH rw`
deliberately exposes the same Git working tree read-write at `/team`, allowing
agents to edit their own roster, roles, policy, or Dockerfile. Such a Dockerfile
edit affects the image selected by the next operator-initiated
`cyclo run` or `cyclo refresh`; it never changes the image of an already
running container.

## Optional derived team image

Docker's standard composition mechanism is image inheritance through `FROM`;
there is no general Dockerfile include or multiple-inheritance operation. Cyclo
therefore supplies a compatible team-runtime base image, and an optional team
Dockerfile describes only the additional packages or artifacts that team
needs.

The required shape is:

```dockerfile
# syntax=docker/dockerfile:1

ARG CYCLO_TEAM_BASE=cyclo-team-base-required
FROM ${CYCLO_TEAM_BASE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iverilog \
        verilator \
        yosys \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir cocotb==2.0.0
```

Docker permits a global `ARG` before `FROM`, so Cyclo can provide the exact
compatible base reference at build time. In a multi-stage Dockerfile, the final
stage must use `FROM ${CYCLO_TEAM_BASE}`; earlier builder stages may use other
images. The team should not hard-code a floating Cyclo base tag, replace the
inherited runtime entrypoint, remove AgentWS or Pi, or install a separate
provider transport. The completed image must still satisfy the Cyclo
team-runtime ABI.

No API key, provider credential, subscription file, gateway store, project
directory, Docker socket, or other host secret is supplied to this build.
Docker receives the team repository as an ordinary build context and honors
its `.dockerignore`; Docker build arguments are not a secret mechanism.

See Docker's official documentation for
[`ARG` and `FROM`](https://docs.docker.com/reference/dockerfile/) and
[multi-stage builds](https://docs.docker.com/build/building/multi-stage/).

## Image identity and lifecycle

The team definition and execution image have related but distinct identities:

- the **team generation** is the Git commit plus the live roster, roles, and
  optional team policy digest;
- the **image generation** is the exact ID of the last successfully built team
  image, labelled with the exact Cyclo-compatible base image ID.

An instance records both. Docker alone interprets `.dockerignore` and decides
whether a changed file affects the build cache.

The accepted build lifecycle is:

1. Resolve the installed Cyclo team base to an immutable image reference.
2. Give Docker the team repository as its normal build context.
3. Build under a temporary candidate tag.
4. Validate the completed image against the team-runtime ABI.
5. Promote the expected tag only after the candidate succeeds.
6. Record and run the exact resulting image ID.

An ordinary `cyclo run` asks Docker to build the common runtime and each
selected derived image. Docker applies the applicable `.dockerignore` or
`Dockerfile.dockerignore` and reuses cached work. Cyclo passes the exact common
base image ID, validates the completed candidate, and transactionally promotes
it before starting any team. `cyclo refresh` stops and restarts the selected
system through the same build path. Only the latest successfully promoted image
is operational state; Cyclo keeps no registry of historical local builds.

Running or refreshing a team with a Dockerfile authorizes Cyclo to execute that
repository's build through the Docker daemon and is therefore a
host-administration action. Review the repository exactly as an installed
provider component. Runtime container isolation cannot make an unsafe build
recipe safe to execute on the host.

## Reusable toolchain layers

Organizations may maintain ordinary compatible intermediate images:

```text
Cyclo team runtime
        |
        v
organization RTL toolchain
        |
        v
specific RTL team
```

Cyclo may pass the approved RTL toolchain image as `CYCLO_TEAM_BASE`, provided
that image still satisfies the same team-runtime ABI. This is a normal,
single-parent Docker inheritance chain. Multi-stage builds are appropriate for
compiling and copying artifacts into the final image, but they do not merge the
installed package state of several parent images.

Cyclo should not invent a parallel `packages.conf` language. `apt`, `pip`,
`npm`, compiler stages, version pinning, and distribution-specific setup remain
ordinary Dockerfile concerns. Keep generated files and `.git` out of the
context with `.dockerignore`.
