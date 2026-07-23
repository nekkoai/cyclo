# Changelog

All notable changes to Cyclo are documented in this file.

## [0.2.0] - 2026-07-18

Composable providers, explicit project authority, and a redesigned operator
interface.

### Architecture

- Define a small component model around declared interfaces and ConnectRPC over
  Unix sockets. Every component provides health; provider components also
  provide model discovery and opaque streaming inference.
- Keep `cyclo` on the host as the control plane. It assembles and inspects the
  Docker graph but is not a service in the inference data path.
- Make the credential gateway the fixed root provider. It alone owns the
  credential volume, OAuth refresh, native model calls, the concrete catalogue,
  and usage history.
- Add ordered intermediate providers through `/etc/cyclo/host.conf`. Each
  component receives only its declared upstream socket; an empty configuration
  exposes the gateway directly.
- Transport Pi's native JSON request and event payloads opaquely. Intermediate
  providers do not interpret, validate, or reserialize inference contents.

### Projects and teams

- Add strict, line-oriented `project.cyclo` files containing a project name,
  description, one or more team repositories, and named `ro`/`rw` mounts.
  Relative paths resolve beside the project file and unknown directives fail.
- Start one container per selected team. Writable mounts appear below
  `/workspace`, read-only supporting material below `/readonly`, and team
  repositories remain independently selectable as read-only or writable.
- Generate a host-path-free `/agentws/PROJECT.md` so every agent receives the
  same concise description of its logical filesystem and project authority.
- Keep team behavior repository-defined: a roster, role prompts, optional
  common `AGENTS.md`, and an optional Dockerfile for extra execution
  dependencies. Cyclo supplies the AgentWS runtime and Pi provider extension as
  the inherited `CYCLO_TEAM_BASE`.
- Build an installation-scoped derived image for each team Dockerfile. Image
  identity records the exact common base; candidate images are validated and
  promoted transactionally.
- Preserve AgentWS's durable task, job, comment, result, retry, and planner
  recovery loop without coupling queue state to provider health.

### Lifecycle and isolation

- Build component images under candidate tags and promote the official tag only
  after validation. Readiness verifies the exact image ID, ownership, launch
  configuration, mounts, running state, dependency readiness, and the component
  health RPC.
- Run intermediate providers without a network, Docker socket, or Linux
  capabilities, using read-only roots, private namespaces, bounded process and
  file-descriptor counts, and only the Unix sockets declared by their interface.
- Keep credentials outside all team and intermediate-provider mounts. A mounted
  provider socket is the authority to use the configured model catalogue; no
  internal bearer or administrator token is used.
- Validate real, non-overlapping mount trees and recheck bind-source identity at
  launch. Multi-team runs preflight the full project and roll back only the
  containers started by a failed invocation.
- Add `--offline` to remove ordinary team network egress while retaining access
  to the provider socket.
- Harden persisted state with strict parsing, no-follow file access, atomic
  replacement, serialized queue mutations, bounded scans, and explicit handling
  of corrupt or incomplete instance records.
- Keep configuration restart-applied: lifecycle and diagnostic commands report
  stale, down, unhealthy, and uninspectable states without silently rebuilding
  or repairing the system.
- Scope gateway, provider, and team Docker resources to the canonical state
  root, allowing several independent Cyclo installations on one trusted host.
  Team ownership records the installation, resource kind, and logical instance.

### Command line and operations

- Organize authoring under `cyclo team` and `cyclo project`, task operations
  under `cyclo task`, and the two independent host lifecycles under
  `cyclo gateway` and `cyclo providers`.
- Add project-wide `run` and `stop`, detailed `inspect`, read-only fleet and
  AgentWS dashboards, provider-aware `models`, retained `usage`, and an
  observational full-system `doctor`.
- Add `cyclo refresh` to rebuild installed gateway, provider, and team images,
  then restart active projects from their persisted definitions.
- Keep gateway login limited to updating the private store; an explicit gateway
  restart publishes catalogue changes while preserving the separately owned
  credential and usage volume.
- Keep wildcard dashboard binds as listening addresses only; generated browser
  links derive their host from the incoming request.

### Distribution

- Establish 0.2 as a fresh-install boundary. Cyclo does not adopt or migrate
  0.1 state or Docker resources.
- Organize shipped component sources by architectural role under
  `src/cyclo/components`: shared contracts in `protocol/`, runnable providers
  beside them, and an explicit `team-runtime` image context. Remove the retired
  gateway and provider-runtime source trees.
- Ship a self-contained Python package with the AgentWS runtime, dashboard,
  component protocol, gateway and team build contexts, and built-in team
  templates. Cyclo no longer depends on external `agentws` or `multiagent`
  checkouts.
- Pin image bases and npm dependencies, verify release manifests and supply-chain
  metadata, scan the Git history for secrets, and build local wheel/source
  release bundles without publishing them.

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
