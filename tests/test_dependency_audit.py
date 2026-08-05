from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "dependency-audit"
AUDIT = runpy.run_path(str(TOOL), run_name="cyclo_dependency_audit_test")
AuditPolicyError = AUDIT["AuditPolicyError"]


def advisory(package: str, url: str, severity: str) -> dict[str, str]:
    return {
        "name": package,
        "dependency": package,
        "severity": severity,
        "url": url,
    }


def exact_report() -> dict[str, object]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "brace-expansion": {
                "name": "brace-expansion",
                "severity": "high",
                "via": [
                    advisory("brace-expansion", url, severity)
                    for url, severity in sorted(
                        AUDIT["WAIVED_ADVISORIES"]["brace-expansion"]
                    )
                ],
                "nodes": [AUDIT["waived_node"]("brace-expansion")],
            },
            "undici": {
                "name": "undici",
                "severity": "high",
                "via": [
                    advisory("undici", url, severity)
                    for url, severity in sorted(
                        AUDIT["WAIVED_ADVISORIES"]["undici"]
                    )
                ],
                "nodes": [AUDIT["waived_node"]("undici")],
            },
            "protobufjs": {
                "name": "protobufjs",
                "severity": "moderate",
                "via": [],
                "nodes": [
                    "node_modules/@earendil-works/"
                    "pi-coding-agent/node_modules/protobufjs"
                ],
            },
        },
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 1,
                "high": 2,
                "critical": 0,
                "total": 3,
            }
        },
    }


def exact_package_files(directory: Path) -> tuple[dict[str, object], dict[str, object]]:
    package = {"dependencies": {AUDIT["PI_PACKAGE"]: AUDIT["PI_VERSION"]}}
    lock = {
        "packages": {
            "": {"dependencies": {AUDIT["PI_PACKAGE"]: AUDIT["PI_VERSION"]}},
            AUDIT["PI_LOCK_PATH"]: {
                "version": AUDIT["PI_VERSION"],
                "hasShrinkwrap": True,
            },
            AUDIT["waived_node"]("brace-expansion"): {"version": "5.0.7"},
            AUDIT["waived_node"]("undici"): {"version": "8.5.0"},
            "node_modules/brace-expansion": {"version": "5.0.9"},
            "node_modules/undici": {"version": "8.9.0"},
        }
    }
    write_package_files(directory, package, lock)
    return package, lock


def write_package_files(
    directory: Path, package: dict[str, object], lock: dict[str, object]
) -> None:
    directory.mkdir(exist_ok=True)
    (directory / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (directory / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")


def test_exact_pi_shrinkwrap_exception_is_accepted(tmp_path: Path) -> None:
    package_dir = tmp_path / "team"
    exact_package_files(package_dir)

    AUDIT["validate_team_component"](exact_report(), 1, package_dir)


@pytest.mark.parametrize(
    "case",
    (
        "brace-extra-node",
        "undici-extra-node",
        "brace-extra-advisory",
        "undici-extra-advisory",
        "undici-advisory-severity",
        "critical",
        "missing-finding",
        "successful-exit",
        "package-pin",
        "lock-pin",
        "no-shrinkwrap",
        "brace-nested-version",
        "undici-nested-version",
        "independent-brace-version",
        "independent-undici-version",
    ),
)
def test_pi_exception_fails_closed_on_policy_drift(
    tmp_path: Path, case: str
) -> None:
    package_dir = tmp_path / "team"
    package, lock = exact_package_files(package_dir)
    report = exact_report()
    returncode = 1

    brace = report["vulnerabilities"]["brace-expansion"]
    undici = report["vulnerabilities"]["undici"]
    if case == "brace-extra-node":
        brace["nodes"].append("node_modules/brace-expansion")
    elif case == "undici-extra-node":
        undici["nodes"].append("node_modules/undici")
    elif case == "brace-extra-advisory":
        brace["via"].append(
            advisory(
                "brace-expansion", "https://example.invalid/advisory", "high"
            )
        )
    elif case == "undici-extra-advisory":
        undici["via"].append(
            advisory("undici", "https://example.invalid/advisory", "moderate")
        )
    elif case == "undici-advisory-severity":
        undici["via"][0]["severity"] = "low"
    elif case == "critical":
        brace["severity"] = "critical"
        report["metadata"]["vulnerabilities"]["high"] = 1
        report["metadata"]["vulnerabilities"]["critical"] = 1
    elif case == "missing-finding":
        report["vulnerabilities"].pop("brace-expansion")
        report["metadata"]["vulnerabilities"]["high"] = 1
        report["metadata"]["vulnerabilities"]["total"] = 2
    elif case == "successful-exit":
        returncode = 0
    elif case == "package-pin":
        package["dependencies"][AUDIT["PI_PACKAGE"]] = "0.82.1"
    elif case == "lock-pin":
        lock["packages"][""]["dependencies"][AUDIT["PI_PACKAGE"]] = "0.82.1"
    elif case == "no-shrinkwrap":
        lock["packages"][AUDIT["PI_LOCK_PATH"]]["hasShrinkwrap"] = False
    elif case == "brace-nested-version":
        lock["packages"][AUDIT["waived_node"]("brace-expansion")][
            "version"
        ] = "5.0.8"
    elif case == "undici-nested-version":
        lock["packages"][AUDIT["waived_node"]("undici")]["version"] = "8.8.0"
    elif case == "independent-brace-version":
        lock["packages"]["node_modules/brace-expansion"]["version"] = "5.0.8"
    elif case == "independent-undici-version":
        lock["packages"]["node_modules/undici"]["version"] = "8.8.0"

    if case in {
        "package-pin",
        "lock-pin",
        "no-shrinkwrap",
        "brace-nested-version",
        "undici-nested-version",
        "independent-brace-version",
        "independent-undici-version",
    }:
        for path in package_dir.iterdir():
            path.unlink()
        write_package_files(package_dir, package, lock)

    with pytest.raises(AuditPolicyError):
        AUDIT["validate_team_component"](report, returncode, package_dir)


def test_unexpected_high_finding_is_rejected(tmp_path: Path) -> None:
    package_dir = tmp_path / "team"
    exact_package_files(package_dir)
    report = exact_report()
    report["vulnerabilities"]["unexpected"] = {
        "name": "unexpected",
        "severity": "high",
        "via": [],
        "nodes": ["node_modules/unexpected"],
    }
    report["metadata"]["vulnerabilities"]["high"] = 3
    report["metadata"]["vulnerabilities"]["total"] = 4

    with pytest.raises(AuditPolicyError):
        AUDIT["validate_team_component"](report, 1, package_dir)


@pytest.mark.parametrize(
    "output",
    (
        "not JSON",
        '{"error":{"summary":"registry unavailable","detail":""}}',
        '{"auditReportVersion":1,"vulnerabilities":{},"metadata":{}}',
    ),
)
def test_malformed_or_failed_npm_audit_is_rejected(output: str) -> None:
    with pytest.raises(AuditPolicyError):
        AUDIT["parse_audit_output"](output, source="fixture")


def test_latest_pi_probe_expires_the_waiver_when_fixed() -> None:
    affected = {
        "packages": {
            "node_modules/brace-expansion": {"version": "5.0.9"},
            "node_modules/pi/node_modules/brace-expansion": {"version": "5.0.7"},
            "node_modules/pi/node_modules/undici": {"version": "8.5.0"},
        }
    }
    result = AUDIT["validate_no_fixed_pi_release"]("0.82.1", affected)
    assert result == {"brace-expansion": ["5.0.7"], "undici": ["8.5.0"]}

    brace_fixed = copy.deepcopy(affected)
    brace_fixed["packages"][
        "node_modules/pi/node_modules/brace-expansion"
    ]["version"] = "5.0.9"
    with pytest.raises(AuditPolicyError):
        AUDIT["validate_no_fixed_pi_release"]("0.83.0", brace_fixed)

    undici_fixed = copy.deepcopy(affected)
    undici_fixed["packages"]["node_modules/pi/node_modules/undici"][
        "version"
    ] = "8.9.0"
    with pytest.raises(AuditPolicyError):
        AUDIT["validate_no_fixed_pi_release"]("0.83.0", undici_fixed)

    with pytest.raises(AuditPolicyError):
        AUDIT["validate_no_fixed_pi_release"]("0.83.0", {"packages": {}})


def test_security_policy_names_the_exact_audit_exception() -> None:
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    for package in AUDIT["WAIVED_VERSIONS"]:
        assert AUDIT["waived_node"](package) in policy
    assert "https://github.com/advisories/GHSA-mh99-v99m-4gvg" in policy
    assert "https://github.com/advisories/GHSA-rgw5-rvv9-x895" in policy
    assert "https://github.com/advisories/GHSA-4cwx-7wf7-3272" in policy
    assert "TEMPORARY WAIVER" in (
        ROOT / "docs" / "releasing.md"
    ).read_text(encoding="utf-8")
