from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src" / "cyclo" / "vendor_gateway" / "runtime_context"
GATEWAY = ROOT / "src" / "cyclo" / "vendor_gateway" / "gateway_context"


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
        "@earendil-works/pi-coding-agent": "0.80.6",
        "pi-lens": "3.8.68",
        "pi-simplify": "0.2.2",
        "pi-web-access": "0.13.0",
    }
    assert package["private"] is True
    assert package["dependencies"] == expected
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == expected
    for dependency in lock["packages"].values():
        resolved = dependency.get("resolved")
        if resolved:
            assert resolved.startswith("https://registry.npmjs.org/")
            assert dependency.get("integrity")


def test_gateway_node_install_is_locked_to_the_runtime_pi_generation() -> None:
    dockerfile = (GATEWAY / "Dockerfile").read_text(encoding="utf-8")
    package = json.loads((GATEWAY / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((GATEWAY / "package-lock.json").read_text(encoding="utf-8"))

    expected = {"@earendil-works/pi-ai": "0.80.6"}
    assert "npm ci" in dockerfile
    assert re.search(r"^FROM node:22-[^@\s]+@sha256:[0-9a-f]{64}", dockerfile)
    assert package["private"] is True
    assert package["dependencies"] == expected
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == expected
    for dependency in lock["packages"].values():
        resolved = dependency.get("resolved")
        if resolved:
            assert resolved.startswith("https://registry.npmjs.org/")
            assert dependency.get("integrity")


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
    assert "tools/release-acceptance" in release
    assert "docker build --pull" in release
    assert not re.search(r"\bgh\s", release)
    assert "git push" not in release
    assert "api.github.com" not in release
    assert "GITHUB_REF_NAME" not in (ROOT / "tools" / "release-manifest").read_text(
        encoding="utf-8"
    )
