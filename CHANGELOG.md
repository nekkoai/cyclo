# Changelog

All notable changes to Cyclo are documented in this file.

## [0.2.0] - 2026-07-18

Provider composition and runtime-isolation release.

- Add strict, line-oriented `project.cyclo` definitions with a name,
  description, one or more Git-defined teams, and one or more named `ro`/`rw`
  mounts. Writable mounts are projects; read-only mounts are supporting inputs.
  Paths resolve relative to the definition, and unsupported or
  unknown directives—including the reserved `mcp` directive—fail closed.
- Start one isolated instance per project team, expose writable projects at
  `/workspace/<name>` and read-only inputs at `/readonly/<name>`, and generate a
  host-path-free `/agentws/PROJECT.md`
  manifest for every agent. Multi-team starts preflight the complete definition
  and roll back instances started by a failed invocation. Bind-source identity,
  running-instance overlap, and per-launch rollback checks prevent path
  substitution or concurrent instance reuse from crossing that boundary.
- Retain `cyclo run TEAM PROJECT` and `--team-write` as a compatibility
  interface while making `cyclo run project.cyclo` the normal reusable path.
- Separate the credential gateway from the provider runtime: the gateway owns
  credentials, concrete upstream traffic, and usage, while the provider runtime
  owns composition, policy, routing, and team capabilities.
- Default gateway account names to the login provider ID and optionally choose
  another with `cyclo gateway login PROVIDER --as NAME`.
- Compose ordered host providers declared as
  `provider PREFIX PATH INPUT_MODEL... [KEY=VALUE ...]` in
  `/etc/cyclo/host.conf`. A separate provider runtime parses that exact
  read-only file bind once at startup and serves normal requests from an
  immutable in-memory snapshot. Every edit takes effect through
  `cyclo runtime restart` without an image rebuild; a missing or empty file
  passes the gateway's concrete catalogue through unchanged.
- Keep normal runtime requests free of configuration reads, gateway-catalogue
  fetches, and global component probes. Separate acknowledged capability reload
  from gateway-catalogue refresh so revocation does not depend on the gateway.
- Watch only dynamic client/provider authority outside the request path, closing
  the controller-crash revocation gap on a 500 ms polling cadence and failing
  closed on a malformed changed registry; `host.conf` remains restart-only.
- Bound shared-runtime TCP/UDS connections, active work per principal and
  globally, request bodies, and inbound-body time while reserving host control
  admission.
- Give every provider a distinct runtime UDS, give the host a separate private
  admin UDS, and charge nested work to a separate pool keyed by the originating
  project so composition cannot multiply one team's root quota.
- Bind each team capability to the provider runtime's interface on that team's
  private network and drop `CAP_NET_RAW` from team containers, preventing
  cross-team bearer replay even from custom/root images.
- Run each intermediate provider in a networkless container with private
  HTTP/1.1 Unix-socket transport to the provider runtime, two scoped
  capabilities, registration/recovery health verification, durable sanitized
  recovery records, in-memory active routes, and per-dispatch socket-identity
  checks. Provider components never register with or connect to the credential
  gateway.
- Bound registration metadata and durable rewrite frequency, make exact
  re-registration a throttled disk-free lease barrier, rate-limit authenticated
  attempts before reading their bodies, reject concrete/component prefix
  collisions locally, and revoke an old provider before replacement authority
  is published.
- Preserve physical usage attribution by keeping the original team bearer in
  provider-runtime request context and forwarding it to the credential gateway
  for concrete calls; components see only ingress, upstream, and request-context
  capabilities.
- Make shared lifecycle explicit with `cyclo runtime start|stop|restart|status`
  and `cyclo provider build|start|restart|stop|status PREFIX|--all`.
  `provider start` never builds, while `cyclo models` and `cyclo run` never
  start or build shared services or provider containers.
- Recreate the shared gateway without deleting credentials or restarting teams
  with `cyclo gateway restart`.
- Keep `cyclo gateway status` observational with respect to gateway images: it
  validates the existing image and refuses to build or pull one implicitly.
- Scrub exact credential reflections from upstream response headers and
  streaming bodies at the gateway boundary.
- Split the owned team-runtime image from the credential-gateway package and
  package the provider runtime and protocol as first-class Cyclo resources.
- Reap detached engine descendants through per-worker Linux subreapers, treat
  unclean worker exits as team-container failures, use Docker teardown as the
  outer process-tree fence, and capability-gate startup queue recovery with an
  exclusive runtime lifetime lock.
- Report team-container lifecycle separately from shared provider-runtime
  status in `cyclo ps` and the dashboard, including explicit down, stale, and
  uninspectable states.
- Serialize task comments, state transitions, and result publication with a
  persistent per-task mutex while retaining atomic task and file publication.
- On a terminal automatic worker failure, publish one deterministic,
  idempotent planner recovery job before failing the source job; planner
  failures do not recursively create recovery work.
- Refuse control operations when any persisted instance record is corrupt or
  unreadable, while letting the dashboard show readable instances alongside
  explicit source errors.
- Make `cyclo doctor` actively probe both the credential gateway and provider
  runtime before trusting the runtime catalogue.

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
