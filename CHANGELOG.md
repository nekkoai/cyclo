# Changelog

All notable changes to Cyclo are documented in this file.

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
