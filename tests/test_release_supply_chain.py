from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src" / "cyclo" / "components" / "team-runtime"
GATEWAY = ROOT / "src" / "cyclo" / "components" / "gateway"
COMPONENTS = ROOT / "src" / "cyclo" / "components"


def test_runtime_node_install_is_locked_and_avoids_remote_installer_scripts() -> None:
    dockerfile = (RUNTIME / "Dockerfile").read_text(encoding="utf-8")
    package = json.loads((RUNTIME / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((RUNTIME / "package-lock.json").read_text(encoding="utf-8"))

    assert "nodesource" not in dockerfile.lower()
    assert "curl -fsSL" not in dockerfile
    assert "npm ci" in dockerfile
    assert re.search(r"^FROM node:22-[^@\s]+@sha256:[0-9a-f]{64}", dockerfile)
    assert re.search(r"^FROM python:3\.12-[^@\s]+@sha256:[0-9a-f]{64}", dockerfile, re.M)

    expected = {
        "@earendil-works/pi-coding-agent": "0.81.1",
        "pi-lens": "3.8.68",
        "pi-simplify": "0.2.2",
        "pi-web-access": "0.13.0",
    }
    assert package["private"] is True
    assert package["dependencies"] == expected
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == expected
    packages = lock["packages"]
    pi_path = "node_modules/@earendil-works/pi-coding-agent"
    nested_brace = f"{pi_path}/node_modules/brace-expansion"
    assert packages[pi_path]["hasShrinkwrap"] is True
    assert packages[nested_brace]["version"] == "5.0.7"
    assert packages["node_modules/brace-expansion"]["version"] == "5.0.8"
    for dependency in lock["packages"].values():
        resolved = dependency.get("resolved")
        if resolved:
            if not resolved.startswith("file:"):
                assert resolved.startswith("https://registry.npmjs.org/")
                assert dependency.get("integrity")


def test_pi_extension_shares_the_cli_pi_ai_without_replacing_legacy_peers() -> None:
    dockerfile = (RUNTIME / "Dockerfile").read_text(encoding="utf-8")
    runtime_lock = json.loads(
        (RUNTIME / "package-lock.json").read_text(encoding="utf-8")
    )
    extension = json.loads(
        (COMPONENTS / "pi-provider" / "package.json").read_text(encoding="utf-8")
    )
    packages = runtime_lock["packages"]
    cli_path = "node_modules/@earendil-works/pi-coding-agent"
    cli_pi_path = f"{cli_path}/node_modules/@earendil-works/pi-ai"
    legacy_pi_path = "node_modules/@earendil-works/pi-ai"
    cli_version = packages[cli_path]["version"]

    assert packages[cli_pi_path]["version"] == cli_version == "0.81.1"
    assert extension["peerDependencies"]["@earendil-works/pi-ai"] == cli_version
    assert packages[legacy_pi_path]["version"] != cli_version
    assert (
        "/opt/cyclo-agent-tools/lib/node_modules/@earendil-works/"
        "pi-coding-agent/node_modules/@earendil-works/pi-ai"
    ) in dockerfile
    assert (
        "ln -s /opt/cyclo-agent-tools/lib/node_modules/@earendil-works/pi-ai"
        not in dockerfile
    )


def test_gateway_node_install_is_locked_to_the_runtime_pi_generation() -> None:
    dockerfile = (GATEWAY / "Dockerfile").read_text(encoding="utf-8")
    package = json.loads((GATEWAY / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((GATEWAY / "package-lock.json").read_text(encoding="utf-8"))

    assert "npm ci" in dockerfile
    assert "command -v flock" in dockerfile
    assert re.search(r"^FROM node:22-[^@\s]+@sha256:[0-9a-f]{64}", dockerfile)
    assert package["private"] is True
    assert package["dependencies"]["@earendil-works/pi-ai"] == "0.81.1"
    assert "@cyclo/component" in package["dependencies"]
    assert "@cyclo/provider" in package["dependencies"]
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    for dependency in lock["packages"].values():
        resolved = dependency.get("resolved")
        if resolved:
            assert resolved.startswith(("https://registry.npmjs.org/", "file:")) or resolved.startswith("../")
            if not dependency.get("link"):
                assert dependency.get("integrity")


def test_every_shipped_component_has_a_pinned_lock_and_declaration() -> None:
    for name in (
        "protocol/component",
        "protocol/provider",
        "gateway",
        "passthrough",
        "pi-provider",
        "team-runtime",
    ):
        package = COMPONENTS / name
        lock = json.loads((package / "package-lock.json").read_text(encoding="utf-8"))
        assert lock["lockfileVersion"] == 3
        assert (package / "package.json").is_file()
        if name in {"gateway", "passthrough", "team-runtime"}:
            assert (package / "Dockerfile").is_file()
        if name in {"gateway", "passthrough"}:
            assert (package / "component.conf").is_file()


def test_local_protocol_dependencies_match_source_and_image_layouts() -> None:
    expected_dependencies = {
        "@cyclo/component": "file:../protocol/component",
        "@cyclo/provider": "file:../protocol/provider",
    }
    for name in ("gateway", "passthrough", "pi-provider"):
        package = json.loads(
            (COMPONENTS / name / "package.json").read_text(encoding="utf-8")
        )
        assert {
            dependency: package["dependencies"][dependency]
            for dependency in expected_dependencies
        } == expected_dependencies

    for name in ("gateway", "passthrough", "team-runtime"):
        dockerfile = (COMPONENTS / name / "Dockerfile").read_text(encoding="utf-8")
        assert "protocol/component/package.json" in dockerfile
        assert "./protocol/component/" in dockerfile
        assert "protocol/provider/package.json" in dockerfile
        assert "./protocol/provider/" in dockerfile


def test_workflow_actions_are_pinned_to_full_commits() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert {path.name for path in workflows} == {"ci.yml"}

    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.search(r"\buses:\s+([^\s#]+)", line)
            if not match:
                continue
            action = match.group(1)
            assert re.search(r"@[0-9a-f]{40}$", action), (
                f"{workflow.relative_to(ROOT)} has an unpinned action: {action}"
            )

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python -m build --no-isolation" in ci
    assert "python tools/normalize-distributions dist" in ci


def test_ci_uses_and_tests_the_current_component_layout() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / "tools" / "build-release").read_text(encoding="utf-8")

    assert "src/cyclo/_bundle" not in ci
    assert (
        "for package in protocol/component protocol/provider gateway passthrough "
        "pi-provider; do"
    ) in ci
    assert 'npm test --prefix "src/cyclo/components/$package"' in ci
    assert "python3 tools/dependency-audit" in ci
    assert '"$source_tree/tools/dependency-audit"' in release
    assert "npm audit" not in ci
    assert "npm audit" not in release
    for component in ("team-runtime", "gateway", "passthrough"):
        assert f"src/cyclo/components/{component}/Dockerfile" in ci
    assert ci.count("src/cyclo/components\n") >= 3
    assert "ensure_derived(" in ci
    assert "ensure_derived(" in release
    assert "build=True" not in ci
    assert "build=True" not in release


def test_build_backend_dependencies_are_available_in_test_and_release_envs() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_requirements = set(project["build-system"]["requires"])
    dev_requirements = set(project["project"]["optional-dependencies"]["dev"])
    release_inputs = {
        line
        for raw_line in (ROOT / "requirements" / "release.in")
        .read_text(encoding="utf-8")
        .splitlines()
        if (line := raw_line.split("#", 1)[0].strip())
    }
    release_lock = (ROOT / "requirements" / "release.txt").read_text(
        encoding="utf-8"
    )

    assert build_requirements <= dev_requirements
    assert build_requirements <= release_inputs
    for requirement in build_requirements:
        assert re.search(rf"(?m)^{re.escape(requirement)}\s+\\$", release_lock)


def test_release_tooling_is_hash_locked_and_git_remote_free() -> None:
    lock = (ROOT / "requirements" / "release.txt").read_text(encoding="utf-8")
    release = (ROOT / "tools" / "build-release").read_text(encoding="utf-8")
    acceptance = (ROOT / "tools" / "release-acceptance").read_text(encoding="utf-8")

    assert "--hash=sha256:" in lock
    assert "--require-hashes" in release
    assert "--no-isolation" in release
    assert "tools/normalize-distributions" in release
    assert "--no-build-isolation" in acceptance
    assert "requirements/release.txt" in acceptance
    assert "tools/release-manifest" in release
    assert "tools/secret-scan" in release
    assert "git archive --format=tar HEAD" in release
    assert "GIT_DIR=$git_dir GIT_WORK_TREE=$source_tree" in release
    assert 'sh -n "$script" || exit 1' in release
    assert "tools/release-acceptance" in release
    assert "tools/runtime-write-acceptance" in release
    assert "node --test tests/*.mjs" in release
    assert "npm ci --force --ignore-scripts --prefix src/cyclo/components/team-runtime" in release
    assert "docker build --pull" in release
    assert "src/cyclo/components/team-runtime" in release
    assert "gateway" in release
    assert "cyclo gateway providers" in acceptance
    assert "CYCLO_PROVIDER_RUNTIME_IMAGE" not in acceptance
    assert not re.search(r"\bgh\s", release)
    assert "git push" not in release
    assert "api.github.com" not in release
    assert "GITHUB_REF_NAME" not in (ROOT / "tools" / "release-manifest").read_text(
        encoding="utf-8"
    )
