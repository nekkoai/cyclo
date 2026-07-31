# Releasing Cyclo

Cyclo uses semantic versions. `0.2.0` is a stable release; a version below
`1.0.0` does not imply an alpha or preview build. The Python distribution is `cyclo-agent`,
while the command, import package, repository, and image names retain `cyclo`.

Version 0.2 is a fresh-install boundary. Release verification does not migrate
0.1 state or Docker resources. Test 0.2 with a new state root.

## Release boundary

Cyclo and DComp are separate projects:

- the Cyclo wheel contains project, team, provider, gateway, and AgentWS domain
  logic;
- DComp owns container, network, volume, and component lifecycle; and
- Cyclo invokes the separately installed `dcomp` executable through machine API
  version 1.

Cyclo does not vendor or download DComp. Set `CYCLO_DCOMP` to an absolute DComp
executable when it is not on `PATH`. An empty override, a missing executable,
non-JSON version output, or any machine API other than 1 is a release failure.

The release wheel must not contain the superseded Python Docker lifecycle
modules. `tools/release-acceptance` audits both the required DComp-facing
modules and the forbidden legacy paths.

## What CI verifies

Ordinary CI is self-contained and does not fetch an unpublished DComp build. It
performs:

- the complete Python test matrix;
- Node protocol, gateway, provider, and UI tests;
- generated-protocol drift checks;
- dependency and secret scans;
- reproducible wheel and source-distribution checks;
- installed-wheel command, template, project-format, and package-content
  acceptance; and
- credential-free Docker builds for the team, gateway, and pass-through images,
  including the baked AgentWS runtime and a derived-team fixture.

The package job runs `tools/release-acceptance` with
`CYCLO_RELEASE_REQUIRE_DCOMP=0`. This is not an end-to-end runtime claim. The
authoritative local release build requires DComp and runs the API-1/Docker
integration gate.

## Prepare the release commit

Update the version in `pyproject.toml` and `src/cyclo/__init__.py`, the top
entry in `CHANGELOG.md`, and installed-version assertions in release tests.
Then run:

```sh
python3 -m pytest -q
node --test tests/*.mjs
tools/dependency-audit
while IFS= read -r script; do sh -n "$script"; done < <(git grep -l '^#!/bin/sh$')
git diff --check
tools/release-acceptance
```

The last command is the wheel-only acceptance path unless
`CYCLO_RELEASE_REQUIRE_DCOMP=1` is explicitly set.

Build and inspect both Python distributions:

```sh
rm -rf build dist
python3 -m pip install --require-hashes -r requirements/release.txt
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) python3 -m build --no-isolation
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) python3 tools/normalize-distributions dist
python3 -m twine check dist/*
tools/release-acceptance "$PWD"/dist/cyclo_agent-*.whl
tools/release-manifest dist
git status --short
```

## Image acceptance

The team image uses `src/cyclo` as its build context because AgentWS and
`container_runtime.py` are baked into the image. Gateway and pass-through
Dockerfiles continue to use `src/cyclo/components`.

```sh
docker build --pull --build-arg "CYCLO_HOST_UID=$(id -u)" --build-arg "CYCLO_HOST_GID=$(id -g)" -t cyclo-team:0.2.0 -f src/cyclo/components/team-runtime/Dockerfile src/cyclo
docker build --pull -t cyclo-gateway:0.2.0 -f src/cyclo/components/gateway/Dockerfile src/cyclo/components
docker build --pull -t cyclo-passthrough:0.2.0 -f src/cyclo/components/passthrough/Dockerfile src/cyclo/components
PYTHONPATH=src python3 -c 'from pathlib import Path; from cyclo.images import Images; images = Images(); base = images.inspect("cyclo-team:0.2.0"); assert base is not None; root = Path("tests/fixtures/derived-team").resolve(); images.build("cyclo-derived-team:0.2.0", dockerfile=root / "Dockerfile", context=root, build_args=(("CYCLO_TEAM_BASE", base.reference),), labels=(("io.cyclo.team-base", base.id),))'
docker run --rm --network none --entrypoint /bin/sh cyclo-derived-team:0.2.0 -ceu 'test "$(cat /opt/cyclo-derived-team-smoke)" = cyclo-derived-team-ok'
PYTHONPATH=src tools/runtime-write-acceptance cyclo-team:0.2.0
docker run --rm --network none cyclo-gateway:0.2.0 providers
```

`tools/runtime-write-acceptance` uses the image-baked AgentWS tools and
`/usr/local/bin/cyclo-team-runtime`; it never mounts a second runtime tree from
the host. It exercises the generated project/settings mounts, writable and
read-only project authority, worker cleanup, and the queue-only one-shot task
administration path.

## Required DComp integration

Install a compatible DComp locally and verify its machine API before building a
release:

```sh
export CYCLO_DCOMP=/absolute/path/to/dcomp
"$CYCLO_DCOMP" version --json
CYCLO_RELEASE_REQUIRE_DCOMP=1 tools/release-acceptance "$PWD"/dist/cyclo_agent-*.whl
```

The acceptance script requires API 1, creates a disposable empty `host.conf`,
builds and applies the credential gateway through Cyclo and DComp, reads
component status through DComp's JSON machine API, runs `cyclo doctor`, and
destroys the disposable gateway store. Its trap also attempts DComp shutdown
after an interrupted or failed run.

## Build the release bundle

The authoritative release operation is:

```sh
CYCLO_DCOMP=/absolute/path/to/dcomp tools/build-release
```

It requires:

- a clean committed worktree;
- Git and Python 3.10 or newer with `venv`;
- Node.js 22 and npm;
- Docker with a running daemon;
- DComp machine API 1; and
- network access to the configured Python/npm indexes and Docker registry for
  pinned release dependencies and image layers.

The builder archives the exact local commit, installs the hash-locked release
toolchain, runs Python, Node, shell, dependency, secret, package, Docker, baked
runtime, and DComp integration acceptance, and writes:

```text
release/cyclo-agent-0.2.0/
  cyclo_agent-0.2.0-py3-none-any.whl
  cyclo_agent-0.2.0.tar.gz
  SHA256SUMS
  release-manifest.json
  cyclo-agent-0.2.0.spdx.json
```

The wheel and source archive are normalized before checksums and the SBOM are
created. Protocol generation must leave the archived commit byte-for-byte
unchanged. The SPDX SBOM enumerates every shipped Node lockfile.

The high-severity dependency gate is implemented once in
`tools/dependency-audit` and is shared by CI and the local builder. The narrow
`TEMPORARY WAIVER` for Pi documented in `SECURITY.md` remains visible and fails
closed on any policy drift.

The completed bundle is copied to a private sibling staging directory and
published with Linux `renameat2(RENAME_NOREPLACE)`. An interrupted build cannot
expose a partial destination or replace an existing release.

The release tools do not inspect, contact, mutate, tag, push to, or publish
through a Git remote or hosting service. Publication is a separate operator
action. Never replace artifacts for an existing version; release a new patch
version.

## Verify on a clean machine

Copy the complete release bundle and a separately obtained DComp executable to
a disposable Linux host:

```sh
cd ./cyclo-agent-0.2.0
sha256sum --check SHA256SUMS
python3 -m venv /tmp/cyclo-release
. /tmp/cyclo-release/bin/activate
python -m pip install ./cyclo_agent-0.2.0-py3-none-any.whl
export CYCLO_DCOMP=/absolute/path/to/dcomp
"$CYCLO_DCOMP" version --json
cyclo --version
cyclo team templates
```

Use a fresh Cyclo installation root:

```sh
export CYCLO_STATE_ROOT=/tmp/cyclo-0.2-state
mkdir -p "$CYCLO_STATE_ROOT"
: > "$CYCLO_STATE_ROOT/host.conf"
cyclo gateway providers
cyclo gateway login PROVIDER
cyclo models
cyclo component list
cyclo doctor
```

Then initialize a team and project, validate them, and run the project. Cyclo
0.2 has no `run --dry-run`; configuration-only checks are `cyclo validate` and
`cyclo providers check`.
