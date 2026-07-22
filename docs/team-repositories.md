# Team repository contract

A Cyclo team is a Git repository that defines agents, not a service. It does
not implement the Component or Provider interfaces, start a daemon, contain
credentials, or own runtime state. Cyclo consumes the repository as data and
runs it with the system-owned team runtime.

## Implementation status

Cyclo 0.2.0 implements the roster, roles, optional policy, Git generation,
`project.cyclo` selection, and `/team` mount described below.

The optional derived team image described in this document is the accepted
contract for the component-system cutover. Cyclo 0.2.0 does **not** discover or
build a `Dockerfile` from a team repository. It has one run-wide `--image`
tag override. A missing or stale selected tag, as well as an explicit
`--build`, builds Cyclo's packaged team-runtime context into that tag. A
manually derived tag may therefore be replaced after the packaged base changes;
do not treat `--image` as the per-team build mechanism described below.

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
  Dockerfile             # optional execution-image delta; accepted design
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
| `/etc/cyclo/host.conf` | Installation-wide provider composition and available models |
| Team repository | Agent roster, role behavior, optional team policy, and optionally the execution-image delta |
| `project.cyclo` | Which teams run, which directories they receive, and each mount mode |
| Cyclo state root | Tasks, jobs, comments, results, transcripts, Pi state, generated runtime files, and instance metadata |

After the component-system cutover, one generic team container will receive:

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
edit never authorizes or triggers a host build; it is inert until an operator
explicitly builds a new image.

## Optional derived team image

Docker's standard composition mechanism is image inheritance through `FROM`;
there is no general Dockerfile include or multiple-inheritance operation. Cyclo
therefore supplies a compatible team-runtime base image, and an optional team
Dockerfile describes only the additional packages or artifacts that team
needs.

The required shape is:

```dockerfile
# syntax=docker/dockerfile:1

ARG CYCLO_TEAM_BASE
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
compatible base reference at build time. The team should not hard-code a
floating Cyclo base tag, replace the inherited runtime entrypoint, remove
AgentWS or Pi, or install a separate provider transport. The completed image
must still satisfy the Cyclo team-runtime ABI.

No API key, provider credential, subscription file, gateway store, project
directory, Docker socket, or other host secret is supplied to this build.
Docker build arguments are not a secret mechanism. A `.dockerignore` should
exclude `.git`, generated files, local state, and anything else the build does
not require.

See Docker's official documentation for
[`ARG` and `FROM`](https://docs.docker.com/reference/dockerfile/) and
[multi-stage builds](https://docs.docker.com/build/building/multi-stage/).

## Image identity and lifecycle

The team definition and execution image have related but distinct identities:

- the **team generation** is the Git commit plus the live roster, roles, and
  optional team policy digest;
- the **image generation** is the exact Cyclo-compatible base image ID plus the
  effective Dockerfile/build-context digest.

An instance must record both. Changing prompts must not imply an image rebuild,
and changing packages must not be hidden inside the prompt generation.

The accepted build lifecycle is:

1. Resolve the installed Cyclo team base to an immutable image reference.
2. Digest that exact base and the effective team build context.
3. Build under a temporary candidate tag.
4. Validate the completed image against the team-runtime ABI.
5. Promote the expected tag only after the candidate succeeds.
6. Record and run the exact resulting image ID.

An ordinary run reuses the recorded current image. A missing or stale derived
image requires a separate, explicit trusted build action; simply running a
team, or allowing it to modify its repository, must not execute its Dockerfile.
Only the latest successfully promoted image is operational state. Cyclo does
not need a registry of every historical local build.

Building a team Dockerfile executes repository-controlled code through the
Docker daemon and is therefore a host-administration action. Review it exactly
as an installed provider Dockerfile. Runtime container isolation cannot make an
unsafe build recipe safe to execute on the host.

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
ordinary Dockerfile concerns.
