# Cyclo user guide

This guide is the complete operational reference for installing, configuring,
running, and maintaining Cyclo. For the project overview and shortest path to a
first team, start with the [main README](../README.md).

Cyclo runs Git-defined agent teams against directories selected by a
`project.cyclo` definition. Each team gets its own Docker container and
filesystem job loop. Writable projects are mounted at `/workspace/<name>`;
read-only supporting inputs are mounted at `/readonly/<name>`. Model
requests enter a shared provider runtime; only concrete calls continue to the
separate credential gateway, so API keys and subscription credentials never
enter a team or provider-component container.

Cyclo is a standalone distribution. It includes the queue runtime, agent
launcher, read-only team viewer, provider runtime, credential gateway, Docker
build contexts, example teams, and dashboard that it needs. It does **not**
install, import, discover, or require another project, and it never expects
sibling checkouts under the user's home directory.

Version 0.2.0 is Cyclo's current stable release; 0.1.0 was the first stable
release. The Python distribution is named `cyclo-agent`; the installed command,
Python package, repository, and product name remain `cyclo` and Cyclo.

```text
                               project.cyclo
                         teams + mounts + modes
                           /               \
                          v                 v
              team Git repositories   mounted directories
              team + roles/*.md       projects + read-only inputs
                           \               /
                            +------+------+
                                   v
                    cyclo-runtime team containers
                      one independent instance/team
                    filesystem tasks/jobs + agents
                    writable scoped Pi runtime state
                               |
                    scoped model capability
                               |
                               v
               cyclo-provider-runtime container
               catalogue + composition + policy
                         |                 |
                         |                 v
                         |       networkless provider components
                         v
                    cyclo-gateway container
                    credentials + concrete proxy + usage
                         |                 |
                         v                 v
             cyclo-gateway-store     concrete model providers
       credentials, subscriptions,
          and retained usage history
```

## Requirements and installation

Cyclo requires:

- a Linux host (Cyclo currently relies on Linux/POSIX state locking, UID/GID
  mapping, bind mounts, and container networking);
- Python 3.10 or newer, with `venv`/`ensurepip` support;
- Git;
- Docker with a running daemon that the current user can access.

Node.js and npm are not host runtime requirements. The JavaScript Pi agent and
provider libraries are installed and run inside Cyclo's Docker images. They are
needed on the host only by maintainers running the complete source and release
test suites.

On a fresh machine, clone Cyclo into its own virtual environment and run the
environment check:

```sh
git clone https://github.com/glguida/cyclo.git
cd cyclo
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
sudo install -d -m 0755 /etc/cyclo
cyclo --version
```

If the source tree is already present, the `git clone` step is unnecessary.
The exact canonical `/etc/cyclo/host.conf` file is mounted read-only into the
provider runtime when present; its siblings are not mounted. An absent or empty
file selects concrete-catalogue pass-through.

After the stable release has been published to a configured Python package
index, install it by distribution name:

```sh
python3 -m pip install cyclo-agent==0.2.0
cyclo --version
```

Release wheels use the normalized distribution filename:

```sh
python3 -m pip install ./cyclo_agent-0.2.0-py3-none-any.whl
cyclo --version
```

An editable installation is useful when developing Cyclo itself:

```sh
python3 -m pip install -e .
```

The source tree or wheel is the complete application. No external agent-runtime
package, executable, environment variable, checkout, or network fetch is part
of Cyclo's Python runtime dependency chain.

## Images, services, and the gateway data volume

Cyclo does not claim that prebuilt images are published. Version 0.2 carries
three owned Docker build contexts inside the installed package:

| Resource | Default name | Purpose |
|---|---|---|
| Team runtime image | `cyclo-runtime:0.2.0` | Pi, local tools, and the per-team loop |
| Provider runtime image | `cyclo-provider-runtime:0.2.0` | Merged catalogue, composition policy, and virtual routing |
| Credential gateway image | `cyclo-gateway:0.2.0` | Login, concrete model proxying, credential substitution, and usage |
| Gateway data volume | `cyclo-gateway-store` | Provider credentials, subscriptions, and retained usage history |
| Host configuration | `/etc/cyclo/host.conf` | Ordered host-wide provider composition; concrete pass-through when absent or empty |
| Team container | `cyclo-<instance>` | One isolated running team/project-definition binding |
| Team network | `cyclo-<instance>-net` | One private network per binding |
| Provider container | `cyclo-provider-<prefix>` | One host-wide intermediate provider |
| Provider transport | private Unix socket directories | HTTP between one networkless component and the provider runtime |

The shared gateway container and network are named
`cyclo-gateway-<state-id>` and `cyclo-gateway-net-<state-id>`, where
`<state-id>` is a stable short digest of the Cyclo state root. This prevents two
independent state roots from accidentally sharing controller state. The
provider runtime joins that runtime–gateway network and separately joins active
team networks; the gateway never joins a team network. Teams share access to
the gateway service through the provider runtime, but no team mounts the
credential volume. Each receives a separate scoped capability.

Development builds made before gateway networks carried scoped ownership
labels require a one-time cleanup. Cyclo refuses to guess that an unlabelled
Docker resource is safe to adopt or delete; the error names the exact legacy
container or network. Stop all Cyclo processes, remove only those named
resources with `docker rm -f` / `docker network rm`, then retry. The credential
volume is separate and is not removed by this migration.

Gateway discovery/login builds its packaged image when it is missing or stale.
Shared provider-runtime and intermediate-provider lifecycle is explicit:
`cyclo runtime start --build` builds the runtime image on its first start, and
`cyclo provider build PREFIX|--all` builds component images. `cyclo models`
never builds or starts anything. `cyclo run` may build its own team-runtime
image when needed, but never provisions a shared service or provider.

Use `cyclo gateway restart --build`, `cyclo runtime restart --build`,
`cyclo provider restart PREFIX --build`, or `cyclo run --build` when you
explicitly want the corresponding rebuild. A `host.conf` edit is data, not an
image change, and does not cause any rebuild. Apply every such edit without
`--build`, using `cyclo runtime restart`.

Names can be overridden deliberately:

```sh
cyclo --gateway-image my-cyclo-gateway:dev \
  --store-volume my-cyclo-credentials gateway status
cyclo --provider-runtime-image my-cyclo-provider-runtime:dev runtime start
cyclo run --image my-cyclo-runtime:dev project.cyclo
```

The corresponding environment variables are `CYCLO_GATEWAY_IMAGE`,
`CYCLO_PROVIDER_RUNTIME_IMAGE`, `CYCLO_GATEWAY_STORE`, and
`CYCLO_RUNTIME_IMAGE`.

## Provision the gateway

Provision credentials through Cyclo's own gateway command:

```sh
cyclo gateway providers
cyclo gateway login openai-codex --as codex-work
cyclo gateway login anthropic --as claude-work
cyclo gateway login github-copilot --as copilot-work
cyclo gateway status
cyclo gateway restart
cyclo runtime start --build
cyclo models
cyclo doctor
```

`cyclo gateway providers` does not mount or read the gateway credential store,
so it works before the first provider login. It prints `PROVIDER`,
`DESCRIPTION`, `AUTH`, and `LOGIN` columns, including a plain-language
explanation and a copyable login command for every provider in the gateway's
built-in Pi registry. The `PROVIDER` login argument selects an upstream AI
service or subscription adapter and is also the default account/catalogue name
used in `PROVIDER/model`. Optional `--as NAME` chooses a different name, for
example when provisioning multiple accounts of one provider. `NAME` uses only
lowercase letters, numbers, underscore, or hyphen. `AUTH` is the default login
route: OAuth entries use `cyclo gateway login PROVIDER`; API-key entries use
`cyclo gateway login PROVIDER --api-key-stdin`.

If the gateway image is absent, Cyclo builds its packaged image first. That
build may need registry and package-index access, but Cyclo passes it no
provider credentials.

This command lists built-ins only. Custom providers depend on the host Pi
`models.json`, so they cannot be discovered before that configuration is
available; they can still be provisioned explicitly with an API key.

Subscription providers use an interactive browser OAuth login. For an API-key
provider, prefer standard input or an environment-variable handoff:

```sh
cyclo gateway login openai --as openai-work --api-key-stdin
cyclo gateway login openai --as openai-work --api-key-env OPENAI_API_KEY
```

OpenAI Codex asks whether to use browser login or a device code. Browser login
is the default; choose device code for a headless or remote machine.

The provisioning command starts a one-shot gateway container and writes the
result directly into `cyclo-gateway-store`. The long-running gateway mounts the
same volume. Cyclo does not copy those credentials into provider-runtime state,
team repositories, projects, provider components, or team containers. The
volume also contains the append-only usage ledger used for experiment
accounting.

Concrete gateway catalogue names come from provisioned accounts: by default
the name is `PROVIDER`, and `--as NAME` overrides it independently of the
upstream provider type. The separate provider runtime adds configured virtual
prefixes to that concrete catalogue.

`cyclo gateway status` lists provisioned accounts without credential material.
It validates and uses the existing local gateway image, mounts the store
read-only in a networkless one-shot inspector, and never builds or pulls an
image. If that image is missing or stale, explicitly run
`cyclo gateway restart --build`.
`cyclo gateway restart` recreates only the Cyclo-owned credential gateway and
preserves its volume. It does not read `host.conf`, manage provider routes, or
restart the provider runtime, provider components, or teams.
Use `cyclo gateway restart --build` to rebuild its image first. In-flight model
requests can fail during the brief replacement and are not retried by Cyclo.
When upgrading the gateway lock protocol, do not run an older Cyclo controller
concurrently. If several state roots share one store, run the new
`cyclo gateway restart --build` once for each state root; an early attempt may
retire its selected old gateway and then fail closed on the next stale peer, so
finish the other state roots and retry it.
After a successful gateway login or restart, Cyclo asks a running provider
runtime to refresh its concrete catalogue through an authenticated control
operation; no provider-runtime restart is needed.
The provider runtime connects to the gateway over their shared private network;
start it explicitly after the gateway with `cyclo runtime start --build` on a
fresh installation. Runtime start fails closed if the gateway image/config is
stale or the gateway has any extra Docker-network attachment; it never changes
the gateway on your behalf.

`cyclo models` is a non-lifecycle refresh-and-query operation against the
already-running provider runtime: it replaces the in-memory concrete-catalogue
snapshot, then prints the
exact concrete and composed `provider/model` values accepted in a team roster.
The gateway must be available for the refresh. The command never starts,
builds, replaces, or reconciles a shared service. If no models are available,
run `cyclo gateway providers` and then the listed
`cyclo gateway login PROVIDER` command for the provider you want. The listed
command uses the default provider name; add `--as NAME` if you want a different
account/catalogue name.

### Supported authentication

| Service | Gateway login provider | Authentication |
|---|---|---|
| Anthropic Claude | `anthropic` | Claude Pro/Max; interactive browser OAuth |
| OpenAI Codex | `openai-codex` | ChatGPT Plus/Pro; interactive browser OAuth |
| GitHub Copilot | `github-copilot` | Copilot subscription; interactive browser OAuth |
| API-key convenience | `openai`, `google`, `xai`, `groq`, `mistral`, `deepseek`, `cerebras`, `openrouter`, `fireworks`, `zai`, `moonshotai`, `huggingface` | Conventional environment variable, standard input, or explicit key argument |

The account's subscription and provider policy determine which models it can
actually use. Other providers may appear in the underlying model catalogue but
can require provider-specific configuration. On each installation, treat the
live output of `cyclo models` as authoritative for roster model names.

## Host provider definitions

The credential gateway owns credentials, concrete account discovery, concrete
proxying, and physical usage accounting. The separate provider runtime owns
public model scope, virtual routes, and composition. Optional host-wide
provider definitions belong in `/etc/cyclo/host.conf`, not in a team repository
or JSON registry:

```text
# provider PREFIX PATH INPUT_MODEL... [KEY=VALUE ...]
provider fusion ./providers/fusion codex-work/MODEL_ID mode=balanced
```

`PREFIX` names the output namespace. `PATH` is a local directory containing a
`Dockerfile`; relative paths resolve from the directory containing `host.conf`,
never from the caller's working directory. At least one exact
`provider/model` input is required. Inputs precede unique, lowercase
component-owned `key=value` parameters, and the complete tail is passed to the
image entrypoint.

Lines are dependency order. A line may consume a concrete gateway model or an
output from an earlier line, allowing pipelines and DAGs without an `input`
keyword. Forward references, cycles, unknown models, duplicate prefixes, and
catalogue collisions fail closed.

The runtime bind-mounts exactly the canonical `host.conf` file read-only and
parses it once at startup. Every edit—including an in-place write, creation or
removal, symlink retarget, or inode replacement—requires an explicit runtime
restart before it takes effect. Use `cyclo runtime restart`; no image rebuild is
needed. A missing or empty file exposes the concrete gateway catalogue
unchanged. Configuration never implicitly builds, starts, restarts, or stops
containers. Select another file with the global `--host-config PATH` option.

Startup produces one immutable in-memory routing snapshot from that
configuration, the concrete gateway catalogue, expected provider state, and
validated persisted registration records. Normal catalogue and inference
requests use the snapshot without rereading files, rebuilding the catalogue, or
probing every component. A successful component registration probes that
component and atomically replaces the snapshot. Host capability reload and
gateway-catalogue refresh are separate authenticated snapshot updates, so a
gateway outage cannot block capability revocation. An unacknowledged revocation
stops the runtime fail-closed; a failed catalogue refresh retains the previous
snapshot.

Client and expected-provider files are dynamic authority, unlike `host.conf`.
Cyclo normally replaces them atomically and waits for the runtime reload's
`204`. As a controller-crash backstop, the runtime compares their file
identities every 500 ms and applies the same reload only when they change. A
malformed changed authority file revokes all dynamic clients and component
routes until it is repaired. The watcher never reads or applies `host.conf`.

The shared runtime bounds hostile workload before reading a model body: eight
active root requests per project/provider and 24 globally, plus a separate
nested pool charged to the originating project (16 per origin, 32 globally).
No more than 12 root bodies and 24 nested bodies are retained globally. Bodies
remain limited to 16 MiB and have a separate 30-second inbound deadline. TCP is
limited to 32 connections per team-facing interface and 256 globally. Each
provider has its own UDS listener capped at 64 connections; host control has a
different mode-`0600` UDS listener, so neither team nor provider saturation can
block revocation. Team-facing interfaces are rate-limited to 500 requests/s and
each provider transport to 200 requests/s, both with an equal burst allowance.

### Provider lifecycle

Operate the shared runtime and configured provider containers explicitly:

```sh
cyclo runtime start --build          # first start
cyclo runtime status

cyclo provider build --all
cyclo provider start --all
cyclo provider status --all

cyclo provider restart fusion       # reuse current image
cyclo provider restart fusion --build
cyclo provider stop fusion
cyclo runtime restart
cyclo runtime stop
```

Every provider subcommand takes either one `PREFIX` or `--all`. `provider
start` never builds. `runtime start` and `runtime restart` rebuild only when
`--build` is present. Omitting a prefix from an operation never stops it as a
side effect. `cyclo models` refreshes and queries the running runtime, and
`cyclo run` requires that runtime; neither command manages shared-service lifecycle.
`cyclo doctor` validates persisted instance records, the host configuration,
provider paths, packaged runtimes, and Docker availability without starting
anything. It also actively
probes the running credential gateway and provider runtime before reading the
runtime catalogue; a cached catalogue cannot make a dead service appear
healthy.

`provider restart` revokes and acknowledges the old prefix before stopping its
container, rotates both provider-local capabilities, then publishes and
launches the replacement. It never grants the
replacement generation or input scope to the old process, and removal of the
old recovery record prevents a same-generation socket from being treated as a
mere renewal.

### Provider container protocol

The normative third-party contract is
[Cyclo provider protocol v1](provider-protocol.md). Cyclo starts the OCI
entrypoint with the input and parameter words as argv and supplies:

```text
CYCLO_PROVIDER_PROTOCOL=1
CYCLO_PROVIDER_PREFIX=<PREFIX>
CYCLO_PROVIDER_GENERATION=<build-context-and-arguments digest>
CYCLO_PROVIDER_RUNTIME_SOCKET=/run/cyclo/runtime/runtime.sock
CYCLO_PROVIDER_SOCKET=/run/cyclo/self/provider.sock
CYCLO_PROVIDER_TOKEN_FILE=/run/secrets/cyclo-provider-token
CYCLO_UPSTREAM_TOKEN_FILE=/run/secrets/cyclo-upstream-token
```

The provider token authenticates registration and runtime-to-component
inference. The upstream token authenticates component-to-runtime catalogue and
inference calls and is scoped to the exact declared inputs. Both are read-only
files; neither is a team bearer or physical credential. Components run with
`--network none`, a read-only root, reduced privileges, resource limits, no
Docker socket, and no team, project, or gateway-volume mount.

The component listens on `CYCLO_PROVIDER_SOCKET`, exposes exact `GET /health`
semantics, and registers with
`PUT /_cyclo/v1/providers/PREFIX` over
`CYCLO_PROVIDER_RUNTIME_SOCKET`. The provider runtime verifies its startup
configuration and expected launch state, probes the component socket, sanitizes
its model metadata, persists the sanitized registration solely for validated
restart recovery, and atomically updates its active in-memory snapshot. On
restart it admits that record only after revalidation and another health probe.
Registration bodies are limited to 64 KiB and 256 models. Changed durable
registrations are rate-limited per prefix and globally; components retry HTTP
429 with bounded backoff. Authenticated attempts are serialized and limited to
ten per second per prefix; an exact renewal is limited to one per second. An
exact idempotent 204 advances only an in-memory dispatch lease and performs no
health probe, disk rewrite, or snapshot rebuild.
The gateway is not involved in registration. Normal model and catalogue
requests do not health-probe all providers; inference rechecks the selected
socket's pinned device/inode at dispatch.

For inference, the team calls the provider runtime over TCP. A virtual route is
forwarded to the component socket with the provider token and a live
`X-Cyclo-Request-Context`. The component calls declared inputs back through the
runtime socket using its upstream token and that context:

```text
team -> provider runtime -> output component
output component -> provider runtime -> declared input
provider runtime -> credential gateway -> concrete service
```

The runtime keeps the original team bearer only in live request context. When
the selected input is concrete, it forwards that bearer to the gateway, which
preserves team/project-generation usage attribution while swapping in the real
credential. Components see only ingress/upstream/context capabilities; they
never see the team bearer or a real credential.

## Team repository

A team is a Git repository with this layout:

```text
my-team/
  team
  roles/
    planner.md
    implementer.md
    reviewer.md
  AGENTS.md              # optional; Cyclo's bundled protocol is the fallback
```

`team` is a whitespace-delimited roster with an explicit proxy model for every
agent. Existing compatible repositories may call the roster `default.team`;
new Cyclo repositories use `team`:

```text
# <name> <role> <agent-engine> <provider/model>
planner-1     planner     pi             codex-work/MODEL_ID
builder-1     implementer pi             codex-work/MODEL_ID
reviewer-1    reviewer    pi-interactive claude-work/MODEL_ID
```

The runtime supports the `pi` and `pi-interactive` engines. The model name is
passed unchanged to Pi and resolved against the provider runtime's merged
catalogue projected into that instance.
Each role must have a matching `roles/<role>.md` file.
At least one agent must have the `planner` role because every submitted task
starts with a planner job. Team-definition files must be regular UTF-8 files;
Cyclo rejects definition-file and `roles/` directory symlinks before reading or
hashing them on the host, and limits each definition file to 1 MiB.

Cyclo records a team generation consisting of its Git commit and a digest of
the live roster, role files, and optional protocol. Experiments therefore
remain attributable when the team definition has uncommitted edits.

## Create a team and project

List the example loops installed with Cyclo:

```sh
cyclo templates
```

Create a standalone team repository from one of them. Replace the model with
an exact value from `cyclo models`:

```sh
cyclo init ~/teams/plan-execute-verify \
  --template plan-execute-verify \
  --model codex-work/MODEL_ID
git -C ~/teams/plan-execute-verify add .
git -C ~/teams/plan-execute-verify commit -m "Define Cyclo team"
cyclo validate ~/teams/plan-execute-verify
```

Omit `--template` to create Cyclo's generic default team. `cyclo init`
initializes the destination as a Git repository unless `--no-git` is passed;
it never writes into an existing non-empty directory.

The normal run unit is a `project.cyclo` file. For example, create
`~/experiments/my-project/project.cyclo` containing:

```text
name my-project
description Implement and independently review my project.
team ../../teams/plan-execute-verify ro
mount source ../../src/my-project rw
mount specifications ../../references/my-project ro
```

Its complete line grammar is:

```text
name <project-name>
description <free text to end of line>
team <directory> <ro|rw>
mount <mount-name> <directory> <ro|rw>
```

Exactly one `name`, one `description`, at least one `team`, and at least one
`mount` are required. Blank lines and whole-line `#` comments are ignored;
there is no quoting or inline-comment syntax. Relative team and mount paths
resolve from the directory containing `project.cyclo`, not the shell's current
directory. They must be existing directories, and selected team/mount trees
must be unique and non-overlapping. Every team must be a Git team repository.
Each `ro` or `rw` token is mandatory: it controls `/team` for that team, or
selects the writable-workspace/read-only-input namespace for a mount.

Writable mount names become paths below `/workspace`; read-only mount names
become paths below `/readonly`. The example exposes `/workspace/source` and
`/readonly/specifications`. Both namespace parents are read-only and contain
only declared mount names. Path
tokens are unquoted and cannot contain whitespace, `~`, comma, quotes, or
backslash. Unknown directives fail closed. In particular, `mcp` is rejected
because this Cyclo version does not yet implement MCP server attachment. See
the exact [`project.cyclo` format](project-format.md) for identifier, file, and
run-option constraints.

Validate the whole definition and start its teams:

```sh
cyclo validate ~/experiments/my-project/project.cyclo
cyclo run ~/experiments/my-project/project.cyclo
```

Cyclo checks its bundled loop ABI, requires the shared provider runtime to be
running, preflights every selected team and mount, updates scoped client
records, materializes a read-only queue runtime and host-path-free project
manifest, and starts one team container per `team` line. It may build the
team-runtime image when needed, but it never starts or builds the gateway,
provider runtime, or provider components. If a later team fails to start,
Cyclo rolls back instances already started by that invocation.

Every team receives the same mounted directories but its own `/team`, queue,
viewer, network, and model capability. Add another line such as
`team ../../teams/auditor ro` to run another independent team. Instance names
combine the project name and team repository name, for example
`my-project-plan-execute-verify` and `my-project-auditor`. The containers run
detached by default; use `cyclo logs -f INSTANCE` for an individual team.
`--foreground` and an explicit `--port` are intentionally rejected for a
multi-team definition because they do not identify one instance.

Submit a task specification to the filesystem loop:

```sh
$EDITOR /tmp/change-001.md
cyclo task my-project-plan-execute-verify change-001 /tmp/change-001.md
cyclo logs -f my-project-plan-execute-verify
```

A task specification describes work in the logical project and can name its
projects or input mount names without knowing host paths. Every agent receives
`/agentws/PROJECT.md` in its initial prompt; that manifest lists writable
`/workspace/<name>` paths and read-only `/readonly/<name>` paths and remains authoritative even when the
team supplies its own `AGENTS.md`.

The original two-path command remains a compatibility form:

```sh
cyclo run TEAM PROJECT
```

It starts one team with `/team` read-only and the single `/workspace` project
read-write. `--name` and `--team-write` apply only to this compatibility form.
In `project.cyclo`, the project name and every access mode come from the file,
so those two overrides are rejected.

A task is the durable objective. Agents claim role-matching jobs, record their
work and evidence in ordinary files, and create follow-up jobs for the next
role. The per-agent worker continues waiting for later work. Tasks, jobs,
comments, transcripts, and results survive container restarts in Cyclo's host
state. Each task has a persistent mutex. Task publication and each individual
file replacement are atomic, while comment, state, and result operations hold
that mutex across their complete multi-file update so concurrent mutations
cannot interleave. Multi-file updates are serialized, not crash-transactional.

Agent retries are bounded to protect experiments from runaway model spend. A
job gets three model-process attempts by default; an unfinished final attempt
does not silently strand the task: for a non-planner job, the worker first
publishes a deterministic, idempotent planner recovery job for the same task,
then marks the source job failed. A retry verifies and reuses that recovery job
instead of duplicating it. Planner failures do not recurse. This automatic path
does not replace the normal AgentWS protocol: an agent that explicitly calls
`job-fail` remains responsible for creating any required follow-up work.
Process
exits that safely settle queue state use capped exponential backoff, and an
agent is suspended after five consecutive settled retry exits. An unclean
worker exit instead restarts the same team container without rebuilding it;
startup recovers active jobs before launching replacement workers.
Workers act as Linux child subreapers, so a clean local retry first terminates
and reaps detached engine descendants. Startup recovery is authorized by the
runtime's exclusive queue lifetime lock; agents cannot request an all-active
reset while the team is running.
SIGINT/SIGTERM shutdown does not consume a job attempt. The advanced controls
are documented in the bundled `tools/README.md`. Set any of
`AGENTWS_MAX_JOB_ATTEMPTS`, `AGENTWS_MAX_CONSECUTIVE_FAILURES`,
`AGENTWS_RETRY_INITIAL_SECONDS`, or `AGENTWS_RETRY_MAX_SECONDS` in the
environment of `cyclo run`; Cyclo forwards only this retry-control allowlist to
the team container, where the values are range-checked before agents start.

Workers also check the shared provider runtime before claiming work. Runtime
maintenance or a runtime crash therefore leaves queued work pending. If the
runtime disappears immediately after a claim or during an engine invocation,
the worker restores the previous attempt count, releases the job, and waits for
runtime health without advancing its suspension circuit breaker.

## Observe and operate

```sh
cyclo ps
cyclo dashboard
cyclo path my-project-plan-execute-verify
cyclo usage
cyclo repair
cyclo stop my-project-plan-execute-verify
```

`cyclo ps` reports container lifecycle (`running`, `stopped`, `stale`, or
`orphan`) separately from provider-runtime status. A running team is `ready`
only when the provider-runtime container is running with its current
configuration and image. Otherwise it is `runtime-down`, `runtime-stale`, or
`runtime-unknown`; an instance outside the normal active-running lifecycle is
`inactive`. This is a shared-runtime prerequisite check, not a synthetic model
request or proof that every upstream is reachable; use `cyclo doctor` for the
broader host check.
`cyclo path` prints the ordinary filesystem queue tree for direct inspection.
Queue mutations, such as task creation, are sent into the running container;
Cyclo does not execute container-writable queue files on the host.

`cyclo dashboard` starts a read-only fleet view on a random loopback port by
default and prints its URL. It shows the same separate lifecycle and provider-
runtime states as `cyclo ps`, plus bounded queue summaries, recent task/job
activity, and gateway token usage for every instance.
Queue scans are limited per refresh to 4,096 direct entries and 2 MiB of file
data, with eight recent tasks and twelve recent activity records retained;
truncation is reported. If the gateway is unavailable, fleet and queue data
remain visible while usage is marked unavailable. The dashboard never starts
or reconciles the gateway and never executes queue content:

```sh
cyclo dashboard
# Cyclo dashboard: http://127.0.0.1:49152/
```

Use `cyclo dashboard --port 4173` for a stable port. To expose it deliberately
on every IPv4 interface, run `cyclo dashboard --host 0.0.0.0`; Cyclo prints a
warning when the selected address is not loopback. It runs in the foreground
until Ctrl-C. Version 0.2 has no application authentication, so only use a
non-loopback bind on a trusted network with appropriate firewall controls. Each
online instance links to its detailed read-only queue viewer; stopped and
offline instances remain visible from persistent host state.

Cyclo never silently omits a corrupt or unreadable `instances/*/run.json`.
Commands that require a complete instance inventory fail closed and identify
the bad source. The dashboard remains useful during repair: it shows every
readable instance and reports each invalid source separately.

`cyclo usage` prints an aggregate JSON snapshot of the retained gateway ledger.
Records contain attribution and provider-reported accounting metadata; Cyclo
does not record request prompts or model responses in that ledger. The gateway
records the concrete upstream provider/model requests it serves and attributes
them to the scoped team/project binding and generation. The gateway ledger is
append-only in 0.2.0 and has no automatic retention limit.
Long-running, high-volume installations should monitor the
`cyclo-gateway-store` volume. Save an aggregate snapshot before destroying the
store:

```sh
cyclo usage > cyclo-usage-before-destroy.json
```

This is an aggregate report, not a raw-ledger backup.

After a Docker daemon restart, the gateway has its own restart policy. If a
team network was changed manually, `cyclo repair` republishes provider-runtime
client capabilities, reattaches the already-running provider runtime to active
team networks, revokes stale capabilities, and removes team containers left by
an interrupted stop. It does not start or rebuild a shared service. `cyclo
stop` preserves the instance's queue history but revokes its model capability
and removes its team runtime and network. If an agent worker dies without a
clean settlement, Cyclo exits the team runtime and Docker destroys that
container's complete process tree. Docker then restarts the same container;
before agents launch, startup resets persisted `claimed` and `running` jobs to
`pending` and clears stale agent assignments.

Use `cyclo gateway restart` for an intentional gateway replacement. It does not
destroy credentials or restart teams, the provider runtime, or components. Use
the separate `cyclo runtime ...` and `cyclo provider ...` commands for those
lifecycles. `gateway` remains the precise name for the credential-holding
concrete proxy.

## Included team loops

The wheel and source installation both contain these templates:

- `plan-execute-verify`: a general evaluator/optimizer loop;
- `test-driven-repair`: reproduce, test, repair, judge, and integrate;
- `adversarial-audit`: parallel read-only inspection and adversarial challenge.

Create any of them with:

```sh
cyclo init DESTINATION --template NAME --model PROVIDER/MODEL
```

The copied directory is a normal, independent team repository. It can be
edited, committed, forked, or allowed to self-modify; it has no path or runtime
link back to the template inside Cyclo. See the README created with each team
for its loop and recommended mount/network modes.

## Mount and network modes

For a `project.cyclo` run, filesystem authority is explicit in the definition
and network/UI authority remains a run option:

| Setting | Team repository | Mounted directories | Network / UI |
|---|---|---|---|
| `team PATH ro` or `team PATH rw` | selected mode for that team | unchanged | unchanged |
| `mount NAME PATH rw` | unchanged | writable project at `/workspace/NAME` | unchanged |
| `mount NAME PATH ro` | unchanged | read-only input at `/readonly/NAME` | unchanged |
| default run | as declared | as declared | direct egress; loopback viewer |
| `--offline` | as declared | as declared | model proxy only; no per-team host viewer |

The per-team viewer binds to `127.0.0.1` by default. To expose a team's
read-only viewer deliberately on every IPv4 interface, pass `--host 0.0.0.0`:

```sh
cyclo run --host 0.0.0.0 ~/experiments/my-project/project.cyclo
```

AgentWS has no application authentication in 0.2.0, so use a non-loopback
bind only on a trusted network with appropriate firewall controls.

Allow a team to edit its own roles or roster by declaring that team writable:

```text
team ../../teams/self-editor rw
mount source ../../src/project rw
```

Those changes stay in the team's Git working tree and are picked up on the
next run. Give an auditor a read-only source snapshot without direct internet egress:

```text
team ../../teams/auditor ro
mount source-snapshot ../../src/project ro
```

```sh
cyclo run --offline ~/experiments/audit/project.cyclo
```

Every team in one definition receives the same mounted directories. Concurrent
writers of a project under `/workspace` therefore have ordinary filesystem race
conditions. Use separate Git worktrees as distinct writable mounts when changes
should be isolated. An auditor may inspect a snapshot under `/readonly`, but it
does not have a writable project in that configuration.

The compatibility command `cyclo run TEAM PROJECT` retains the old defaults:
the team is read-only and the single `/workspace` project is writable.
`--team-write` changes the legacy team mode; it is rejected with
`project.cyclo` because its team lines already express that authority.

Every instance has a separate Docker network. Cyclo attaches its team runtime
and the provider runtime to that network; the credential gateway stays on its
separate runtime-only network. With `--offline`, the team network is internal:
the provider runtime remains reachable while the team has no direct outbound
route. Each team capability is bound to the provider runtime's local address on
that network, so a bearer copied into another team fails authentication; a
missing runtime attachment fails closed with no token-only fallback. Cyclo also
drops `CAP_NET_RAW` from every team container, including custom images, so a
root process cannot forge a packet for another runtime interface. Offline
mode does not publish the per-team viewer; use `cyclo logs`,
`cyclo path`, or the host dashboard.

An auditor can currently inspect a read-only snapshot and judge its effects.
Cyclo does not expose another team's private tasks, transcripts, or
queue state to an agent team. That requires a separate, explicit read-only
observation interface rather than weakening the filesystem boundary.

## Owned components

Cyclo owns and ships three runtime components:

- The **filesystem agent loop** contains the task/job protocol, queue commands,
  agent launcher, supervisor, and read-only viewer.
- The **provider runtime** contains the merged catalogue, host-provider policy,
  virtual routing, request contexts, and its Docker build context.
- The **credential gateway** contains login/status handling, the scoped-client
  registry, concrete model projection, credential substitution, concrete
  proxy/usage service, and its Docker build context.

All three are Cyclo source code. They are maintained, packaged, and released in
this repository; nothing is cloned, installed, imported, searched for, or
mounted from another source checkout.

Local provider directories named explicitly in `/etc/cyclo/host.conf` are
operator-owned build inputs, not source dependencies that Cyclo clones or
discovers. Cyclo builds and launches only the directories explicitly named in
that file.

Cyclo has no source-repository lock or external runtime manifest. Docker image
labels are computed directly from build contexts shipped in the installed
package. They detect stale images without referring to another repository or
revision; shared-service and provider rebuilds remain explicit lifecycle
operations.

## Security boundary

The team container receives:

- its team repository;
- the writable workspaces and read-only inputs declared in `project.cyclo`;
- writable task, job, agent, and transcript state;
- writable per-instance Pi runtime state containing only its projected model
  config and scoped provider-runtime capability. Pi needs this tree for lock
  files and other local runtime metadata.

It does **not** receive:

- provider credentials or subscription files;
- the gateway or provider-runtime administrator token, or credential volume;
- the host Pi configuration directory;
- the Docker socket;
- the host home directory;
- another team's `/team` repository, private queue, or Docker network.

The scoped capability is additionally bound to the provider runtime interface
on this team's private network. Teams in one `project.cyclo` intentionally
share its writable workspaces and read-only inputs, but never their team repositories or queue
state. Cyclo rejects overlaps among declared team/project trees and mounts
covering Cyclo's state, embedded controller/runtime source, the host Pi
directory, or the Docker socket. The scoped token protects credentials and
provides attribution; it is not general data-loss prevention. An agent that can
read project data and call an allowed model can send that data to the model
provider. Non-model web egress is available unless `--offline` is used.

Writable Git trees are untrusted output. A `rw` workspace or team allows an
agent to change repository-local configuration and hooks as well as ordinary
files. Cyclo avoids hook-triggering host-side Git operations, but subsequent
host Git commands should treat agent-produced trees as untrusted.

Gateway usage accounting is per team/project binding and team generation,
concrete provider, and model, not per individual agent. It uses
provider-reported token counters and is intended for experiments rather than
billing reconciliation.

## Persistent state

By default Cyclo uses:

```text
$XDG_STATE_HOME/cyclo/
  control.lock
  gateway/                    # client registry and controller capabilities
  provider-runtime/           # registry, capabilities, registration recovery, sockets
  instances/<instance>/
    run.json                  # project, paths, modes, generation, lifecycle
    runtime/                  # materialized, container-read-only filesystem loop
    workspace/                # inert namespace for writable projects
    readonly/                 # inert namespace for read-only inputs
    agentws-state/            # writable tasks, jobs, agents, and transcripts
    pi/agent/                 # writable projected config, scoped token, Pi state
```

If `XDG_STATE_HOME` is unset, the root is `~/.local/state/cyclo`. Override it
with `CYCLO_STATE_ROOT` or the global `--state-root` option.

Optional host provider definitions are separate system configuration:

```text
/etc/cyclo/host.conf          # provider PREFIX PATH INPUT... [KEY=VALUE ...]
```

This file contains no provider credentials. Relative implementation paths are
resolved from its containing directory, and a missing or empty file selects
concrete-catalogue pass-through. Only the canonical file is mounted read-only
into the provider runtime; provider-runtime state is a separate writable host
bind. Provider capabilities contain no physical credentials. Select another
location with global `--host-config PATH`.

Provider credentials, subscriptions, and retained token-usage history are
outside both host trees in the Docker-managed `cyclo-gateway-store` volume.
Stopping teams or deleting ordinary instance state does not delete this volume.
Gateway data destruction is an explicit administrator action. Export any usage
report you need, then stop all teams and destroy the default store with its name
repeated as confirmation:

```sh
cyclo --store-volume cyclo-gateway-store \
  gateway destroy-store --confirm cyclo-gateway-store
```

This irreversible command deletes **all credentials, subscriptions, and usage
history** in that volume and affects every Cyclo state root sharing it. It stops
and removes every verified Cyclo gateway container that mounts the store, using
immutable container IDs, and then removes the volume. It refuses instead of
guessing if a foreign, unverified, or in-progress provisioning container also
mounts the volume. Repeating the exact volume name is the authorization to
delete the volume itself; Docker volumes created by Cyclo do not carry separate
ownership metadata. If a new container mounts the store after preflight,
Docker's final in-use check preserves the data and reports an error. Any
gateways stopped in that race remain stopped until the explicit
`cyclo gateway restart` command.

For a component map and explicit trust boundaries, see
[`architecture.md`](architecture.md). The exact project-definition contract is
in [`project-format.md`](project-format.md). The local release build and
verification procedure is in [`releasing.md`](releasing.md), and vulnerability
reporting is in [`SECURITY.md`](../SECURITY.md).

## Development

Development also has no sibling-repository setup:

```sh
python3 -m pip install -e .
python3 -m pytest -q
node --test tests/*.mjs
python3 -m cyclo.cli doctor
```

Before publishing a release, run the standalone wheel acceptance check:

```sh
tools/release-acceptance
```

This `/bin/sh` script targets Cyclo's supported Linux hosts. It installs the
hash-locked release toolchain into a temporary build environment, invokes
`pip wheel --no-build-isolation`, and installs the resulting wheel into a
separate temporary virtual environment. It then switches to an empty `HOME`,
removes user-local executables from `PATH`, and exercises the installed `cyclo`
command. It checks packaged templates, team initialization and validation,
`run --dry-run`, the packaged job-loop ABI, and the credential-gateway import.
If Docker is
installed and reachable, `cyclo doctor` must pass completely; otherwise only
the Docker probe is skipped and all wheel checks must still pass. The temporary
environment is removed on success, failure, or interruption. Running the check
requires the standard-library `venv`/`ensurepip` support and package-index
access to install the hash-locked build tools. PEP 517 build isolation is
disabled, so the backend is not downloaded a second time. It probes Docker but
does not build images, provision credentials, or start a team.
