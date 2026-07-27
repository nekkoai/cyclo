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


def exact_report() -> dict[str, object]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "brace-expansion": {
                "name": "brace-expansion",
                "severity": "high",
                "via": [
                    {
                        "name": "brace-expansion",
                        "dependency": "brace-expansion",
                        "severity": "high",
                        "url": AUDIT["WAIVED_ADVISORY"],
                    }
                ],
                "nodes": [AUDIT["WAIVED_NODE"]],
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
                "high": 1,
                "critical": 0,
                "total": 2,
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
            AUDIT["WAIVED_NODE"]: {"version": AUDIT["WAIVED_VERSION"]},
            "node_modules/brace-expansion": {"version": "5.0.8"},
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
    package_dir = tmp_path / "team-runtime"
    exact_package_files(package_dir)

    AUDIT["validate_team_runtime"](exact_report(), 1, package_dir)


@pytest.mark.parametrize(
    "case",
    (
        "extra-node",
        "extra-advisory",
        "critical",
        "clean",
        "successful-exit",
        "package-pin",
        "lock-pin",
        "no-shrinkwrap",
        "nested-version",
        "independent-version",
    ),
)
def test_pi_exception_fails_closed_on_policy_drift(
    tmp_path: Path, case: str
) -> None:
    package_dir = tmp_path / "team-runtime"
    package, lock = exact_package_files(package_dir)
    report = exact_report()
    returncode = 1

    finding = report["vulnerabilities"]["brace-expansion"]
    if case == "extra-node":
        finding["nodes"].append("node_modules/brace-expansion")
    elif case == "extra-advisory":
        finding["via"].append(
            {
                "name": "brace-expansion",
                "dependency": "brace-expansion",
                "severity": "high",
                "url": "https://example.invalid/another-advisory",
            }
        )
    elif case == "critical":
        finding["severity"] = "critical"
        finding["via"][0]["severity"] = "critical"
        report["metadata"]["vulnerabilities"]["high"] = 0
        report["metadata"]["vulnerabilities"]["critical"] = 1
    elif case == "clean":
        report["vulnerabilities"].pop("brace-expansion")
        report["metadata"]["vulnerabilities"]["high"] = 0
        report["metadata"]["vulnerabilities"]["total"] = 1
        returncode = 0
    elif case == "successful-exit":
        returncode = 0
    elif case == "package-pin":
        package["dependencies"][AUDIT["PI_PACKAGE"]] = "0.82.1"
    elif case == "lock-pin":
        lock["packages"][""]["dependencies"][AUDIT["PI_PACKAGE"]] = "0.82.1"
    elif case == "no-shrinkwrap":
        lock["packages"][AUDIT["PI_LOCK_PATH"]]["hasShrinkwrap"] = False
    elif case == "nested-version":
        lock["packages"][AUDIT["WAIVED_NODE"]]["version"] = "5.0.8"
    elif case == "independent-version":
        lock["packages"]["node_modules/brace-expansion"]["version"] = "5.0.7"

    if case in {
        "package-pin",
        "lock-pin",
        "no-shrinkwrap",
        "nested-version",
        "independent-version",
    }:
        for path in package_dir.iterdir():
            path.unlink()
        write_package_files(package_dir, package, lock)

    with pytest.raises(AuditPolicyError):
        AUDIT["validate_team_runtime"](report, returncode, package_dir)


def test_unexpected_high_finding_is_rejected(tmp_path: Path) -> None:
    package_dir = tmp_path / "team-runtime"
    exact_package_files(package_dir)
    report = exact_report()
    report["vulnerabilities"]["unexpected"] = {
        "name": "unexpected",
        "severity": "high",
        "via": [],
        "nodes": ["node_modules/unexpected"],
    }
    report["metadata"]["vulnerabilities"]["high"] = 2
    report["metadata"]["vulnerabilities"]["total"] = 3

    with pytest.raises(AuditPolicyError):
        AUDIT["validate_team_runtime"](report, 1, package_dir)


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
        "packages": {"node_modules/brace-expansion": {"version": "5.0.7"}}
    }
    fixed = copy.deepcopy(affected)
    fixed["packages"]["node_modules/brace-expansion"]["version"] = "5.0.8"

    assert AUDIT["validate_no_fixed_pi_release"]("0.82.1", affected) == ["5.0.7"]
    with pytest.raises(AuditPolicyError):
        AUDIT["validate_no_fixed_pi_release"]("0.83.0", fixed)
    with pytest.raises(AuditPolicyError):
        AUDIT["validate_no_fixed_pi_release"]("0.83.0", {"packages": {}})


def test_security_policy_names_the_exact_audit_exception() -> None:
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert AUDIT["WAIVED_ADVISORY"] in policy
    assert AUDIT["WAIVED_NODE"] in policy
    assert "TEMPORARY WAIVER" in (
        ROOT / "docs" / "releasing.md"
    ).read_text(encoding="utf-8")
