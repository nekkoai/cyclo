from __future__ import annotations

import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import cyclo
import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools" / "release-manifest"
COMPONENT_SOURCES = (
    "protocol/component",
    "protocol/provider",
    "gateway",
    "passthrough",
    "team",
)


def test_release_manifest_scans_every_owned_node_lockfile(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(MANIFEST), run_name="cyclo_release_manifest_test")
    release_root = tmp_path / "release-root"
    expected_lockfiles = {
        "src/cyclo/components/protocol/component/package-lock.json",
        "src/cyclo/components/gateway/package-lock.json",
        "src/cyclo/components/passthrough/package-lock.json",
        "src/cyclo/components/team/pi/package-lock.json",
        "src/cyclo/components/protocol/provider/package-lock.json",
        "src/cyclo/components/team/package-lock.json",
    }
    assert {
        path.as_posix() for path in namespace["NODE_LOCK_PATHS"]
    } == expected_lockfiles

    for relative in namespace["NODE_LOCK_PATHS"]:
        lock_path = release_root / relative
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        dependency_name = f"@cyclo-test/{lock_path.parent.name}"
        lock_path.write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {},
                        f"node_modules/{dependency_name}": {
                            "name": dependency_name,
                            "version": "1.0.0",
                            "license": "MIT",
                            "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    locked_node_packages = namespace["locked_node_packages"]
    locked_node_packages.__globals__["ROOT"] = release_root
    packages = locked_node_packages()
    assert {package["lockfile"] for package in packages} == expected_lockfiles

    sbom = namespace["make_spdx"](
        version="0.2.0",
        commit="0" * 40,
        created="2023-11-14T22:13:20Z",
        wheel_hash="0" * 64,
    )
    dependency_comments = {
        package["comment"]
        for package in sbom["packages"]
        if package["name"] != "cyclo-agent"
    }
    assert dependency_comments == {
        f"Locked dependency in {lockfile}" for lockfile in expected_lockfiles
    }


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory) -> Path:
    dist = tmp_path_factory.mktemp("release-distributions")
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(dist),
        ],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if build.returncode != 0 and "No module named build" in build.stdout:
        pytest.skip("build frontend is not installed")
    assert build.returncode == 0, build.stdout
    return dist


def test_built_distributions_contain_component_sources_without_installs(
    built_distributions: Path,
) -> None:
    component_root = ROOT / "src" / "cyclo" / "components"
    source_files = [component_root / "__init__.py"]
    for component in COMPONENT_SOURCES:
        source_files.extend((component_root / component).rglob("*"))
    expected_components = {
        (Path("cyclo/components") / path.relative_to(component_root)).as_posix()
        for path in source_files
        if path.is_file()
        and "node_modules" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    expected = expected_components

    wheel = next(built_distributions.glob("cyclo_agent-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    assert expected <= wheel_names
    assert not any("node_modules" in PurePosixPath(name).parts for name in wheel_names)
    assert not any("__pycache__" in PurePosixPath(name).parts for name in wheel_names)
    assert not any(PurePosixPath(name).suffix in {".pyc", ".pyo"} for name in wheel_names)

    sdist = next(built_distributions.glob("cyclo_agent-*.tar.gz"))
    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_names = {
            "/".join(PurePosixPath(member.name).parts[2:])
            for member in archive.getmembers()
            if member.isfile()
            and len(PurePosixPath(member.name).parts) >= 3
            and PurePosixPath(member.name).parts[1] == "src"
        }
        archive_names = {member.name for member in archive.getmembers()}
    assert expected <= sdist_names
    assert (
        f"cyclo_agent-{cyclo.__version__}/tests/fixtures/derived-team/Dockerfile"
        in archive_names
    )
    assert (
        f"cyclo_agent-{cyclo.__version__}/tools/publish-release"
        in archive_names
    )
    assert not any(
        "node_modules" in PurePosixPath(name).parts for name in archive_names
    )
    assert not any(
        "__pycache__" in PurePosixPath(name).parts for name in archive_names
    )
    assert not any(
        PurePosixPath(name).suffix in {".pyc", ".pyo"}
        for name in archive_names
    )


def copy_distributions(source: Path, destination: Path) -> None:
    for artifact in source.iterdir():
        if artifact.name.endswith((".whl", ".tar.gz")):
            shutil.copy2(artifact, destination / artifact.name)


def manifest_env(*, commit: str | None = None) -> dict[str, str]:
    if commit is None:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = "1700000000"
    env["CYCLO_RELEASE_COMMIT"] = commit
    return env


def run_manifest(dist: Path, *, commit: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MANIFEST), str(dist)],
        cwd=ROOT,
        env=manifest_env(commit=commit),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_release_manifest_for_built_distributions(
    built_distributions: Path, tmp_path: Path
) -> None:
    copy_distributions(built_distributions, tmp_path)

    result = run_manifest(tmp_path)
    assert result.returncode == 0, result.stdout

    manifest = json.loads((tmp_path / "release-manifest.json").read_text())
    assert manifest["distribution"] == "cyclo-agent"
    assert manifest["version"] == "0.2.0"
    assert manifest["source_date_epoch"] == 1700000000
    assert [artifact["name"] for artifact in manifest["artifacts"]] == sorted(
        artifact["name"] for artifact in manifest["artifacts"]
    )
    checksums = (tmp_path / "SHA256SUMS").read_text()
    assert all(artifact["sha256"] in checksums for artifact in manifest["artifacts"])

    sbom = json.loads((tmp_path / "cyclo-agent-0.2.0.spdx.json").read_text())
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["documentDescribes"] == ["SPDXRef-Package-cyclo-agent"]
    root_package = next(
        package for package in sbom["packages"] if package["name"] == "cyclo-agent"
    )
    assert root_package["downloadLocation"] == "NOASSERTION"
    assert any(
        reference["referenceLocator"] == "pkg:pypi/cyclo-agent@0.2.0"
        for reference in root_package["externalRefs"]
    )
    assert sbom["dataLicense"] == "CC0-1.0"
    assert sbom["documentNamespace"].startswith("https://spdx.org/spdxdocs/")
    package_ids = {package["SPDXID"] for package in sbom["packages"]}
    assert len(package_ids) == len(sbom["packages"])
    known_ids = package_ids | {"SPDXRef-DOCUMENT"}
    for relationship in sbom["relationships"]:
        assert relationship["spdxElementId"] in known_ids
        assert relationship["relatedSpdxElement"] in known_ids
    scoped_purls = [
        reference["referenceLocator"]
        for package in sbom["packages"]
        for reference in package.get("externalRefs", [])
        if reference["referenceLocator"].startswith("pkg:npm/%40")
    ]
    assert scoped_purls


@pytest.mark.parametrize(
    ("pattern", "tampered_name", "error"),
    [
        (
            "cyclo_agent-*.whl",
            "cyclo_agent-0.2.0-malicious-py3-none-any.whl",
            "unexpected wheel filename",
        ),
        (
            "cyclo_agent-*.tar.gz",
            "cyclo_agent-0.2.0-malicious.tar.gz",
            "unexpected source distribution filename",
        ),
    ],
)
def test_release_manifest_requires_exact_artifact_names(
    built_distributions: Path,
    tmp_path: Path,
    pattern: str,
    tampered_name: str,
    error: str,
) -> None:
    copy_distributions(built_distributions, tmp_path)
    artifact = next(tmp_path.glob(pattern))
    artifact.rename(tmp_path / tampered_name)

    result = run_manifest(tmp_path)

    assert result.returncode == 1
    assert error in result.stdout


def test_release_manifest_cross_checks_sdist_metadata(
    built_distributions: Path, tmp_path: Path
) -> None:
    copy_distributions(built_distributions, tmp_path)
    sdist = next(tmp_path.glob("cyclo_agent-*.tar.gz"))
    metadata = (
        b"Metadata-Version: 2.4\n"
        b"Name: cyclo-agent\n"
        b"Version: 9.9.9\n"
        b"Requires-Python: >=3.10\n\n"
    )
    with tarfile.open(sdist, mode="w:gz") as archive:
        member = tarfile.TarInfo("cyclo_agent-0.2.0/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))

    result = run_manifest(tmp_path)

    assert result.returncode == 1
    assert "source distribution metadata does not match wheel metadata" in result.stdout


def test_release_manifest_requires_an_existing_local_commit(
    built_distributions: Path, tmp_path: Path
) -> None:
    copy_distributions(built_distributions, tmp_path)

    result = run_manifest(tmp_path, commit="0" * 40)

    assert result.returncode == 1
    assert "does not exist in the local repository" in result.stdout


def test_release_manifest_requires_the_checked_out_commit(
    built_distributions: Path, tmp_path: Path
) -> None:
    commits = subprocess.run(
        ["git", "rev-list", "--max-count=2", "HEAD"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.splitlines()
    if len(commits) < 2:
        pytest.skip("repository has no parent commit")
    copy_distributions(built_distributions, tmp_path)

    result = run_manifest(tmp_path, commit=commits[1])

    assert result.returncode == 1
    assert "does not match the checked-out source" in result.stdout
