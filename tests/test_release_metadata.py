from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

import cyclo


ROOT = Path(__file__).resolve().parents[1]


def project_metadata() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]


def test_distribution_keeps_the_cyclo_runtime_identity() -> None:
    project = project_metadata()

    assert project["name"] == "cyclo-agent"
    assert project["version"] == cyclo.__version__ == "0.2.5"
    assert cyclo.__name__ == "cyclo"
    assert project["scripts"] == {"cyclo": "cyclo.cli:main"}

    result = subprocess.run(
        [sys.executable, "-m", "cyclo.cli", "--version"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.stdout.strip() == "cyclo 0.2.5"


def test_release_metadata_is_stable() -> None:
    classifiers = project_metadata()["classifiers"]

    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert all("Alpha" not in classifier for classifier in classifiers)


def test_release_documents_and_sdist_manifest_match_the_version() -> None:
    version = cyclo.__version__
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release_guide = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert f"## [{version}]" in changelog
    assert "First stable release." in changelog
    assert "Alpha" not in changelog
    assert f"`{version}` is a stable release" in release_guide
    assert "distribution is `cyclo-agent`" in release_guide
    assert "command, import package, repository," in release_guide

    manifest_entries = set(manifest)
    assert {
        "include CHANGELOG.md",
        "include LICENSE",
        "include README.md",
        "include SECURITY.md",
        "include tools/build-release",
        "include tools/dependency-audit",
        "include tools/normalize-distributions",
        "include tools/publish-release",
        "include tools/release-acceptance",
        "include tools/release-manifest",
        "include tools/runtime-write-acceptance",
        "include tools/secret-scan",
        "recursive-include docs *.md",
        "recursive-include requirements *.in *.txt",
        "recursive-include template README.md team Dockerfile *.md",
        "recursive-include tests *.py *.mjs",
        "include tests/fixtures/derived-team/Dockerfile",
    } <= manifest_entries
