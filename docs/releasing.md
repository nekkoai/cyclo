# Releasing Cyclo

Cyclo uses stable semantic versions. `0.2.0` is a stable release; version
numbers below `1.0.0` do not imply an alpha or preview build. The Python
distribution is `cyclo-agent`, while the command, import package, repository,
and Docker images retain the `cyclo` name.

## Prepare the release commit

Update the version in `pyproject.toml` and `src/cyclo/__init__.py`, image tags
and examples in `README.md`, the installed-version expectations in
`tools/release-acceptance` and `tests/test_release_*.py`, and the top entry in
`CHANGELOG.md`. Then run:

```sh
python3 -m pytest -q
node --test tests/*.mjs
while IFS= read -r script; do sh -n "$script"; done < <(git grep -l '^#!/bin/sh$')
git diff --check
tools/release-acceptance
```

Build from a clean committed tree and validate both distributions:

```sh
rm -rf build dist
python3 -m pip install --require-hashes -r requirements/release.txt
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) \
  python3 -m build --no-isolation
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) \
  python3 tools/normalize-distributions dist
python3 -m twine check dist/*
tools/release-manifest dist
git status --short
```

Build and smoke-test all three credential-free image contexts:

```sh
docker build --pull -t cyclo-team:0.2.0 \
  -f src/cyclo/_bundle/team/Dockerfile \
  src/cyclo/_bundle
docker build --pull -t cyclo-gateway:0.2.0 \
  -f src/cyclo/_bundle/gateway/Dockerfile \
  src/cyclo/_bundle
docker build --pull -t cyclo-passthrough:0.2.0 \
  -f src/cyclo/_bundle/passthrough/Dockerfile \
  src/cyclo/_bundle
docker run --rm --network none \
  -e CYCLO_HOST_UID=1000 -e CYCLO_HOST_GID=1000 \
  cyclo-team:0.2.0 python3 --version
docker run --rm --network none cyclo-gateway:0.2.0 supported-providers.mjs
docker run --rm --network none cyclo-gateway:0.2.0 providers.mjs
```

The worktree must be clean and tests must pass on the exact commit before a
release is built.

## Build the release bundle

The authoritative release operation is local and does not use a Git hosting
service or publish anything:

```sh
tools/build-release
```

It requires Git, Python 3.10 or newer with `venv`/`ensurepip`, Node.js and npm,
Docker with a running daemon, and network access to the configured Python and
npm package indexes and Docker registry. It never reads or changes a Git
remote. Registry access is used only to install the locked verification tools,
run dependency audits, and fetch pinned Docker image/package layers.

The script refuses a dirty tree, archives the exact local commit, installs the
hash-locked release tools into a temporary environment, and disables PEP 517
build isolation so the backend cannot be replaced by an implicit download. It
runs the Python, Node, shell, dependency, clean-wheel, and three-template
acceptance suites, builds and smoke-tests all three credential-free Docker
images, and writes this bundle:

```text
release/cyclo-agent-0.2.0/
  cyclo_agent-0.2.0-py3-none-any.whl
  cyclo_agent-0.2.0.tar.gz
  SHA256SUMS
  release-manifest.json
  cyclo-agent-0.2.0.spdx.json
```

The wheel and source archive are normalized before their checksums and SBOM are
created. Rebuilding the same commit with the same Python version, locked
toolchain, and commit-derived `SOURCE_DATE_EPOCH` produces byte-identical
distribution files. Docker base images and npm tarballs are pinned, but the
runtime image also installs packages from the live Debian index and is not
claimed to be byte-identical across rebuild dates.

The SPDX SBOM enumerates locked Node dependencies from the team runtime,
credential gateway, and provider runtime package locks.

`tools/build-release` does not inspect, contact, mutate, tag, push to, or
publish through any Git remote or hosting service. Publication is deliberately
separate from the build and is not performed by Cyclo's release tooling. Never
replace an artifact for an existing version; publish a new patch version
instead.

## Verify from a clean machine

On a disposable Linux host with Git, Python 3.10 or newer, and Docker, copy the
wheel from the release bundle and first verify its passive commands:

```sh
python3 -m venv /tmp/cyclo-release
. /tmp/cyclo-release/bin/activate
python -m pip install ./cyclo_agent-0.2.0-py3-none-any.whl
cyclo --version
cyclo templates
cyclo gateway providers
cyclo runtime status
```

At this point the provider runtime is deliberately absent. `cyclo doctor` is a
diagnostic and must not build or start it, so a nonzero result that explicitly
reports the absent runtime is expected. `tools/release-acceptance` exercises
that clean installed-wheel state while checking the packaged runtime resources
and Docker daemon independently.

For a complete operational check, provision a disposable provider account and
operate shared services in dependency order:

```sh
sudo install -d -m 0755 /etc/cyclo
cyclo gateway login PROVIDER
cyclo gateway restart
cyclo runtime start --build
cyclo provider build --all   # when /etc/cyclo/host.conf defines providers
cyclo provider start --all   # when /etc/cyclo/host.conf defines providers
cyclo doctor
cyclo models
```

The order is intentional: credential gateway, provider runtime, configured
provider components, then `doctor`. None of `doctor`, `models`, or `run` starts
or rebuilds these shared services.

Initialize one packaged team, run `cyclo validate`, and perform a `run
--dry-run` against a disposable project. Compare the copied wheel against
`SHA256SUMS`; if it differs or installation fails, discard the bundle and build
a corrected patch version.
