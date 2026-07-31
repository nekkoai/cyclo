# Changelog

All notable changes to Cyclo are documented in this file.

## [0.2.0] - 2026-07-31

Cyclo 0.2 introduces DComp-backed composition, explicit project authority, and
a host-oriented operator interface. It is a fresh-install release and does not
adopt Cyclo 0.1 state or Docker resources.

### Component architecture

- Make DComp the sole owner of component container, network, volume, and
  interrupted-operation lifecycle. Cyclo discovers `dcomp` through `PATH` or
  `CYCLO_DCOMP` and requires machine API version 1.
- Compile the gateway, configured Provider components, and all desired-running
  teams into one installation-wide DComp system.
- Keep Cyclo as a host CLI and domain compiler rather than a daemon or
  inference proxy. DComp likewise leaves the data path after component startup.
- Use DComp's direct private TCP link networks for component interfaces.
  Consumers receive only declared `DCOMP_LINK_*` targets; no service registry,
  sidecar, internal administrator token, or Docker socket is required.
- Identify component interfaces by fully qualified protobuf service name.
  Cyclo's built-in components use ConnectRPC over HTTP/1.1 TCP on port 50051.
- Give every configured component a literal, independently inspectable status.
  Make the selected outer Provider the only route; a failed component makes the
  system non-operational until fixed.

### Gateway and Provider composition

- Keep the credential gateway as the fixed root Provider. It alone owns API
  keys, OAuth refresh, native provider calls, the source catalogue, and usage
  history.
- Store credentials and usage in a private named Docker volume never mounted
  into team or intermediate Provider components.
- Define Provider components with `component.dcomp` and install instances from
  the line-oriented `host.conf` grammar:
  `provider NAME SOURCE [context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARG...]`.
- Resolve all host declarations together, allowing explicit forward references,
  fan-out, and cyclic address wiring. Use the last provider declaration as the
  outer Provider; an empty configuration exposes the gateway directly.
- Add a typed `ListModels` control plane and an opaque streaming `Infer` data
  plane. Transport Pi requests and events as exact JSON strings without
  intermediate validation or reserialization.
- Publish only the outer Provider on a dynamic loopback port for host catalogue
  calls. Restrict the host Provider client to the DComp-reported
  `127.0.0.1` endpoint.
- Make gateway login prepare only the fixed gateway/store boundary, commit the
  credential update, and restart the gateway automatically. A broken unrelated
  Provider or team therefore cannot block credential administration.

### Images and lifecycle

- Use stable installation/version tags for gateway, Provider, common-team, and
  per-team images. Invoke Docker build whenever an operation needs a built
  image, rely on Docker's context, `.dockerignore`, and layer cache, then pass
  only the inspected immutable image ID to DComp.
- Keep no Cyclo source-digest cache or generated image-build history. Capture
  Docker build output and surface bounded diagnostics on failure.
- Keep image building in Cyclo and container lifecycle in DComp. Provider
  descriptors may name a prebuilt image or supply a Dockerfile; optional
  `context=PATH` selects a containing build context.
- Materialize only the current content-addressed DComp component descriptors
  under the Cyclo state root and atomically replace `system.dcomp`; obsolete
  descriptors are removed after the new file is selected.
- Resume an incomplete DComp operation before applying current intent. Keep
  DComp's private state below `STATE_ROOT/dcomp` and access it only through the
  versioned machine API.
- Persist team instances as domain state only: immutable image ID, project/team
  generation, mount facts, options, and `running` or `stopped` intent. Remove
  container IDs, network IDs, and the duplicate Cyclo lifecycle state machine.
- Make `repair` apply current host configuration and persisted instance intent,
  including running required host builds. Keep `refresh` as the operation that
  reparses running project/team definitions and rebuilds their selected images.
- Scope the DComp system, generated images, credential volume, instance state,
  and queues to the canonical state-root identity so several installations can
  share one trusted Docker host without resource-name collision.
- Bind each installation to one canonical local Docker Unix endpoint on first
  use and reject later retargeting or remote Docker endpoints.

### Projects and teams

- Add strict, line-oriented `project.cyclo` files containing a name,
  description, optional literal context block, one or more team repositories,
  and one or more named `ro`/`rw` mounts.
- Map writable projects to `/workspace/NAME` and read-only supporting inputs to
  `/readonly/NAME`. Support several writable repositories in one project
  without inventing a privileged primary repository.
- Generate a read-only `/agentws/project.cyclo` for each team with container
  paths and no generated host paths. Require the common agent protocol to read
  it before choosing a workspace.
- Keep team behavior Git-defined through a roster, role prompts, optional
  `AGENTS.md`, and optional Dockerfile.
- Bake AgentWS, Pi, the Provider adapter, and the team supervisor into the
  common image. Bind only durable tasks, jobs, agents, Pi state, team source,
  project context, and declared project directories at runtime.
- Keep all code installed or executed in a team container together under the
  team component, with a separate host-side `cyclo.team` library for parsing,
  image and DComp-definition construction, queue inspection, templates,
  compatibility checks, and administration.
- Support team Dockerfiles through `ARG CYCLO_TEAM_BASE` and a final
  `FROM ${CYCLO_TEAM_BASE}`. Validate the fixed entrypoint, OCI health check,
  base-image identity, and privilege-drop contract.
- Preserve AgentWS tasks, jobs, comments, results, retries, planner
  notifications, and orphan recovery independently of team component
  replacement.
- Run task administration through confined one-shot team-image tools over the
  durable queues, so tasks remain inspectable and editable while a team is
  stopped without starting its long-running component.
- Snapshot task specifications with bounded, no-symlink host reads before
  mounting them into queue-only administration containers.

### Isolation and correctness

- Treat the host, DComp, Docker daemon, approved configuration, and image build
  inputs as the trusted administrative domain; treat arbitrary team code as
  hostile.
- Refuse to build or run teams as host root. Map the invoking UID/GID into the
  common image and drop image-root privileges before AgentWS starts.
- Canonicalize bind sources and reject missing, overlapping, swapped, or
  protected mount trees, including Cyclo state, installed code, `host.conf`,
  DComp, host Pi state, pseudo-filesystems, and Docker sockets.
- Recheck source device/inode identity at initial launch and mount authority on
  every later global apply.
- Keep credentials, Docker control, DComp state, and unrelated project/team
  paths out of team components.
- Add `--offline` to remove direct team egress and viewer publication while
  retaining the private Provider link.
- Preserve gateway credential-reflection suppression without imposing semantic
  validation on the opaque Provider transport.
- Harden instance and queue state with strict parsing, no-follow reads, bounded
  inputs, serialized mutation, atomic replacement, and directory durability.

### Operator interface

- Organize authoring under `cyclo team` and `cyclo project`, work under
  `cyclo task`, team lifecycle under `run/start/stop/refresh/forget`, and host
  inspection under `component`, `providers`, `gateway`, and `doctor`.
- Add `component list|status|logs|restart` over actual DComp components.
- Add `providers check|status|restart` and
  `gateway providers|login|status|restart|build|destroy-store`.
- Add project-wide run/stop, persisted instance start/forget, detailed
  inspection, live component logs, model discovery, usage reporting, and a
  read-only fleet dashboard.
- Make `doctor` observational: report DComp compatibility, expected component
  health, stopped-instance absence, and model-catalogue reachability without
  applying an alternative system.
- Derive browser links from the incoming request host when a viewer listens on
  `0.0.0.0`; never use the wildcard bind as a destination address.

### Distribution

- Ship the gateway, Provider protocol, Pi adapter, common team runtime, bundled
  AgentWS runtime, dashboard, and example team templates in the Python
  distribution.
- Remove runtime dependencies on external `agentws` or `multiagent` checkouts.
  DComp remains a separate required executable with machine API version 1.
- Pin container bases and npm dependencies, validate generated protobuf
  bindings, scan release history for secrets, and build local wheel/source
  bundles with provenance, checksums, and an SPDX SBOM without publishing them.

## [0.1.0] - 2026-07-14

First stable release.

- Define agent teams as Git repositories containing a roster and role prompts.
- Run each team in an isolated Docker container against an explicitly mounted
  project directory.
- Keep provider API keys and subscription credentials in a separate gateway
  container, and give team containers scoped model access.
- Record token usage by team, generation, provider, and model.
- Bundle the filesystem job loop, runtime and gateway build contexts, dashboard,
  and three working team templates in the Python distribution.
- Pin the runtime agent and gateway to Pi 0.80.6, with complete npm integrity
  locks and credential-free extension-loading smoke tests.
- Provide lifecycle, inspection, repair, usage, model-discovery, and environment
  diagnostic commands through the `cyclo` executable.
- Explain built-in gateway providers and list copyable login commands without
  requiring a provider login or mounting the private gateway store.
- Support Pi's interactive OAuth method selector and device-code callback for
  subscription logins, including OpenAI Codex.
- Build a locally verified release bundle containing checksums, provenance
  metadata, an SPDX SBOM, and secret-scan results without publishing it.
