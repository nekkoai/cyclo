# Cyclo

Cyclo runs a Git-defined agent team against a separate project directory. Each
team gets its own Docker container and filesystem job loop. Model requests go
through a separate Cyclo credential-gateway container, so provider API keys and
subscription credentials never enter a team container.

Cyclo is a standalone distribution. It includes the queue runtime, agent
launcher, read-only team viewer, credential gateway, Docker build contexts,
example teams, and dashboard that it needs. It does **not** install, import,
discover, or require another project, and it never expects sibling checkouts
under the user's home directory.

Version 0.1.0 is Cyclo's first stable release. The Python distribution is named
`cyclo-agent`; the installed command, Python package, repository, and product
name remain `cyclo` and Cyclo.

```text
team Git repository                         project directory
team + roles/*.md                           source being worked on
        | /team: read-only by default              | /workspace: writable by default
        +----------------------+-------------------+
                               v
                    cyclo-runtime team container
                    filesystem tasks/jobs + agents
                    writable scoped Pi runtime state
                               |
                        private Docker network
                               |
                               v
                    cyclo-gateway container
                    upstream proxy + usage records
                               |
                    cyclo-gateway-store volume
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
cyclo doctor
```

If the source tree is already present, the `git clone` step is unnecessary.

After the stable release has been published to a configured Python package
index, install it by distribution name:

```sh
python3 -m pip install cyclo-agent==0.1.0
cyclo doctor
```

Release wheels use the normalized distribution filename:

```sh
python3 -m pip install ./cyclo_agent-0.1.0-py3-none-any.whl
cyclo doctor
```

An editable installation is useful when developing Cyclo itself:

```sh
python3 -m pip install -e .
```

The source tree or wheel is the complete application. No external agent-runtime
package, executable, environment variable, checkout, or network fetch is part
of Cyclo's Python runtime dependency chain.

## Images and the gateway data volume

Cyclo does not claim that prebuilt images are published. Version 0.1 builds its
two images from Docker contexts carried inside the installed Cyclo package:

| Resource | Default name | Purpose |
|---|---|---|
| Team runtime image | `cyclo-runtime:0.1.0` | Pi, local tools, and the per-team loop |
| Credential gateway image | `cyclo-gateway:0.1.0` | Login, proxying, model projection, and usage |
| Gateway data volume | `cyclo-gateway-store` | Provider credentials, subscriptions, and retained usage history |
| Team container | `cyclo-<instance>` | One isolated running team/project binding |
| Team network | `cyclo-<instance>-net` | One private network per binding |

The shared gateway container and network are named
`cyclo-gateway-<state-id>` and `cyclo-gateway-net-<state-id>`, where
`<state-id>` is a stable short digest of the Cyclo state root. This prevents two
independent state roots from accidentally sharing controller state. Teams may
share the credential volume while still receiving separate scoped
capabilities.

Development builds made before gateway networks carried scoped ownership
labels require a one-time cleanup. Cyclo refuses to guess that an unlabelled
Docker resource is safe to adopt or delete; the error names the exact legacy
container or network. Stop all Cyclo processes, remove only those named
resources with `docker rm -f` / `docker network rm`, then retry. The credential
volume is separate and is not removed by this migration.

Images are built automatically the first time they are needed. The first
`cyclo gateway ...`, `cyclo models`, or `cyclo run` can therefore take longer
and needs network access to fetch Docker base-image and package layers. Later
runs reuse an image only when its embedded source fingerprint still matches.
Use `cyclo run --build` to force a team-runtime rebuild,
`cyclo run --build-gateway` to force a gateway rebuild, or
`cyclo gateway ... --build` while provisioning credentials.

Names can be overridden deliberately:

```sh
cyclo --gateway-image my-cyclo-gateway:dev \
  --store-volume my-cyclo-credentials gateway status
cyclo run --image my-cyclo-runtime:dev TEAM PROJECT
```

The corresponding environment variables are `CYCLO_GATEWAY_IMAGE`,
`CYCLO_GATEWAY_STORE`, and `CYCLO_RUNTIME_IMAGE`.

## Provision the gateway

Provision credentials through Cyclo's own gateway command:

```sh
cyclo gateway providers
cyclo gateway status
cyclo gateway login openai-codex
cyclo gateway login anthropic
cyclo gateway login github-copilot
cyclo models
```

`cyclo gateway providers` does not mount or read the gateway credential store,
so it works before the first provider login. It prints `PROVIDER`,
`DESCRIPTION`, `AUTH`, and `LOGIN` columns, including a plain-language
explanation and a copyable login command for every provider in the gateway's
built-in Pi registry. A provider is the upstream AI service or subscription
account behind the `provider/model` name. `AUTH` is the default login route:
OAuth entries use `cyclo gateway login PROVIDER`; API-key entries use `cyclo
gateway login PROVIDER --api-key-stdin`.

If the gateway image is absent, Cyclo builds its packaged image first. That
build may need registry and package-index access, but Cyclo passes it no
provider credentials.

This command lists built-ins only. Custom providers depend on the host Pi
`models.json`, so they cannot be discovered before that configuration is
available; they can still be provisioned explicitly with an API key.

Subscription providers use an interactive browser OAuth login. For an API-key
provider, prefer standard input or an environment-variable handoff:

```sh
cyclo gateway login openai --api-key-stdin
cyclo gateway login openai --api-key-env OPENAI_API_KEY
```

OpenAI Codex asks whether to use browser login or a device code. Browser login
is the default; choose device code for a headless or remote machine.

The provisioning command starts a one-shot gateway container and writes the
result directly into `cyclo-gateway-store`. The long-running gateway mounts the
same volume. Cyclo does not copy those credentials into its host state, team
repositories, projects, or team containers. The volume also contains the
append-only usage ledger used for experiment accounting.

`cyclo gateway status` lists provisioned accounts. `cyclo models` starts or
reuses the gateway and prints the exact `provider/model` values accepted in a
team roster. If no models are available, run `cyclo gateway providers` and then
the listed `cyclo gateway login ...` command for the provider you want.

### Supported authentication

| Service | Cyclo provider | Authentication |
|---|---|---|
| Anthropic Claude | `anthropic` | Claude Pro/Max; interactive browser OAuth |
| OpenAI Codex | `openai-codex` | ChatGPT Plus/Pro; interactive browser OAuth |
| GitHub Copilot | `github-copilot` | Copilot subscription; interactive browser OAuth |
| API-key convenience | `openai`, `google`, `xai`, `groq`, `mistral`, `deepseek`, `cerebras`, `openrouter`, `fireworks`, `zai`, `moonshotai`, `huggingface` | Conventional environment variable, standard input, or explicit key argument |

The account's subscription and provider policy determine which models it can
actually use. Other providers may appear in the underlying model catalogue but
can require provider-specific configuration. On each installation, treat the
live output of `cyclo models` as authoritative for roster model names.

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
planner-1     planner     pi             openai-codex/MODEL_ID
builder-1     implementer pi             openai-codex/MODEL_ID
reviewer-1    reviewer    pi-interactive anthropic/MODEL_ID
```

The runtime supports the `pi` and `pi-interactive` engines. The model name is
passed unchanged to Pi and resolved against the gateway's projected model
catalogue. Each role must have a matching `roles/<role>.md` file.
At least one agent must have the `planner` role because every submitted task
starts with a planner job. Team-definition files must be regular UTF-8 files;
Cyclo rejects definition-file and `roles/` directory symlinks before reading or
hashing them on the host, and limits each definition file to 1 MiB.

Cyclo records a team generation consisting of its Git commit and a digest of
the live roster, role files, and optional protocol. Experiments therefore
remain attributable when the team definition has uncommitted edits.

## Create and run a team

List the example loops installed with Cyclo:

```sh
cyclo templates
```

Create a standalone team repository from one of them. Replace the model with
an exact value from `cyclo models`:

```sh
cyclo init ~/teams/plan-execute-verify \
  --template plan-execute-verify \
  --model openai-codex/MODEL_ID
git -C ~/teams/plan-execute-verify add .
git -C ~/teams/plan-execute-verify commit -m "Define Cyclo team"
cyclo validate ~/teams/plan-execute-verify
```

Omit `--template` to create Cyclo's generic default team. `cyclo init`
initializes the destination as a Git repository unless `--no-git` is passed;
it never writes into an existing non-empty directory.

Start the team against a separate project:

```sh
cyclo run --name plan-execute-verify \
  ~/teams/plan-execute-verify \
  ~/src/my-project
```

Cyclo checks its bundled loop ABI, builds missing images, reconciles the gateway,
issues a provider-and-model-scoped capability for this instance, materializes a
read-only queue runtime, and starts the team container. It prints the instance
name, per-team viewer URL, and persistent queue-state path. The container runs
detached by default. With `--foreground`, Ctrl-C stops and removes that team
container and network, and revokes its scoped gateway capability; persistent
queue history remains.

Submit a task specification to the filesystem loop:

```sh
$EDITOR /tmp/change-001.md
cyclo task plan-execute-verify change-001 /tmp/change-001.md
cyclo logs -f plan-execute-verify
```

A task specification describes work relative to the project passed to
`cyclo run`; it never needs to mention Cyclo's internal `/workspace` mount.
After creating a task, Cyclo prints the actual host project root so generated
artifacts are easy to locate.

A task is the durable objective. Agents claim role-matching jobs, record their
work and evidence in ordinary files, and create follow-up jobs for the next
role. The per-agent wrapper continues waiting for later work. Tasks, jobs,
comments, transcripts, and results survive container restarts in Cyclo's host
state.

Agent retries are bounded to protect experiments from runaway model spend. A
job gets three model-process attempts by default; an unfinished final attempt
marks the job failed instead of returning it to the queue forever. Process
restarts use capped exponential backoff, and an agent is suspended after five
consecutive failures until its Cyclo container is restarted. SIGINT/SIGTERM
shutdown does not consume a job attempt. The advanced controls are documented
in the bundled `tools/README.md`. Set any of
`AGENTWS_MAX_JOB_ATTEMPTS`, `AGENTWS_MAX_CONSECUTIVE_FAILURES`,
`AGENTWS_RETRY_INITIAL_SECONDS`, or `AGENTWS_RETRY_MAX_SECONDS` in the
environment of `cyclo run`; Cyclo forwards only this retry-control allowlist to
the team container, where the values are range-checked before agents start.

## Observe and operate

```sh
cyclo ps
cyclo dashboard
cyclo path plan-execute-verify
cyclo usage
cyclo repair
cyclo stop plan-execute-verify
```

`cyclo ps` reports `running`, `stopped`, `stale`, or `orphan` instances.
`cyclo path` prints the ordinary filesystem queue tree for direct inspection.
Queue mutations, such as task creation, are sent into the running container;
Cyclo does not execute container-writable queue files on the host.

`cyclo dashboard` starts a read-only fleet view on a random loopback port by
default and prints its URL. It combines lifecycle state, bounded queue
summaries, recent task/job activity, and gateway token usage for every instance.
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
until Ctrl-C. Version 0.1 has no application authentication, so only use a
non-loopback bind on a trusted network with appropriate firewall controls. Each
online instance links to its detailed read-only queue viewer; stopped and
offline instances remain visible from persistent host state.

`cyclo usage` prints an aggregate JSON snapshot of the retained gateway ledger.
Records contain attribution and provider-reported accounting metadata; Cyclo
does not record request prompts or model responses in that ledger. The ledger
is append-only in 0.1.0 and has no automatic retention limit. Long-running,
high-volume installations should monitor the `cyclo-gateway-store` volume. Save
an aggregate snapshot before destroying the store:

```sh
cyclo usage > cyclo-usage-before-destroy.json
```

This is an aggregate report, not a raw-ledger backup.

After a Docker daemon restart, the gateway has its own restart policy. If a
gateway or network was changed manually, `cyclo repair` reconciles it,
reattaches active private networks, revokes stale capabilities, and removes
Cyclo-owned containers left by an interrupted stop. `cyclo stop` preserves the
instance's queue history but revokes its gateway capability and removes its
team container/network.

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

These controls are independent:

| Setting | Team repository | Project | Network / UI |
|---|---|---|---|
| default | read-only | writable | direct egress; loopback viewer |
| `--team-write` | writable | unchanged | unchanged |
| `--project-read-only` | unchanged | read-only | unchanged |
| `--offline` | unchanged | unchanged | model gateway only; no per-team host viewer |

The default protects the team definition while allowing it to work on the
project. Allow a team to edit its own roles or roster explicitly:

```sh
cyclo run --team-write ~/teams/self-editor ~/src/project
```

Those changes stay in the team's Git working tree and are picked up on the
next run. Run an auditor without project writes or direct internet egress:

```sh
cyclo run --project-read-only --offline ~/teams/auditor ~/src/project
```

Several teams may mount the same project, but concurrent writable teams have
ordinary filesystem race conditions. Use separate Git worktrees when their
changes should be isolated. A read-only auditor can safely inspect a writer's
project tree.

Every instance has a separate Docker network. With `--offline`, that network
is internal: the gateway remains reachable because Cyclo attaches it to the
team's network, while the team has no direct outbound route. Offline mode does
not publish the per-team viewer; use `cyclo logs`, `cyclo path`, or the host
dashboard.

An auditor can currently inspect the same project read-only and judge its
effects. Cyclo does not expose another team's private tasks, transcripts, or
queue state to an agent team. That requires a separate, explicit read-only
observation interface rather than weakening the filesystem boundary.

## Owned components

Cyclo owns and ships the two runtime components it uses:

- The **filesystem agent loop** contains the task/job protocol, queue commands,
  agent launcher, supervisor, and read-only viewer.
- The **credential gateway** contains login/status handling, the scoped-client
  registry, model projection, proxy/usage service, and the two Docker build
  contexts.

Both are Cyclo source code. They are maintained, packaged, and released in this
repository; nothing is cloned, installed, imported, searched for, or mounted
from another source checkout.

Cyclo has no source-repository lock or external runtime manifest. Docker image
cache labels are computed directly from the build contexts shipped in the
installed package, so a changed context causes an image rebuild without
referring to another repository or revision.

## Security boundary

The team container receives:

- its team repository;
- its project mount;
- writable task, job, agent, and transcript state;
- writable per-instance Pi runtime state containing only its projected model
  config and provider-and-model-scoped gateway capability. Pi needs this tree
  for lock files and other local runtime metadata.

It does **not** receive:

- provider credentials or subscription files;
- the gateway administrator token or credential volume;
- the host Pi configuration directory;
- the Docker socket;
- the host home directory;
- another team's filesystem mounts or private Docker network.

Cyclo rejects overlapping team/project mounts and mounts covering Cyclo's
state, embedded controller/runtime source, the host Pi directory, or the Docker
socket. The scoped token protects credentials and provides attribution; it is
not general data-loss prevention. An agent that can read project data and call
an allowed model can send that data to the model provider. Non-model web egress
is available unless `--offline` is used.

Writable Git trees are untrusted output. With the default writable project—or
with `--team-write`—an agent can change repository-local configuration and
hooks as well as ordinary files. Cyclo avoids hook-triggering host-side Git
operations, but subsequent host Git commands should treat agent-produced trees
as untrusted.

Usage accounting is per team/project binding and team generation, provider,
and model, not per individual agent. It uses provider-reported token counters
and is intended for experiments rather than billing reconciliation.

## Persistent state

By default Cyclo uses:

```text
$XDG_STATE_HOME/cyclo/
  control.lock
  gateway/                    # client registry and controller capabilities
  instances/<instance>/
    run.json                  # paths and lifecycle metadata; no provider keys
    runtime/                  # materialized, container-read-only filesystem loop
    agentws-state/            # writable tasks, jobs, agents, and transcripts
    pi/agent/                 # writable projected config, scoped token, Pi state
```

If `XDG_STATE_HOME` is unset, the root is `~/.local/state/cyclo`. Override it
with `CYCLO_STATE_ROOT` or the global `--state-root` option.

Provider credentials, subscriptions, and retained token-usage history are
outside that tree in the Docker-managed `cyclo-gateway-store` volume. Stopping
teams or deleting ordinary instance state does not delete this volume. Gateway
data destruction is an explicit administrator action. Export any usage report
you need, then stop all teams and destroy the default store with its name
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
gateways already stopped in that race are recreated by the next run or by
`cyclo repair`.

For a component map and explicit trust boundaries, see
[`docs/architecture.md`](docs/architecture.md). The local release build and
verification procedure is in [`docs/releasing.md`](docs/releasing.md), and
vulnerability reporting is in [`SECURITY.md`](SECURITY.md).

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
