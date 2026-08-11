from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "dependency-audit"
AUDIT = runpy.run_path(str(TOOL), run_name="cyclo_dependency_audit_test")
AuditPolicyError = AUDIT["AuditPolicyError"]
TOOL_GLOBALS = AUDIT["audit_repository"].__globals__
EXPECTED_PACKAGES = (
    "components/protocol/component",
    "components/protocol/provider",
    "components/gateway",
    "components/passthrough",
    "components/pooler",
    "components/openai",
    "components/team/pi",
    "components/team",
)


def audit_report(
    finding_name: str | None = None, severity: str = "high"
) -> dict[str, object]:
    vulnerabilities: dict[str, object] = {}
    counts = {
        "info": 0,
        "low": 0,
        "moderate": 0,
        "high": 0,
        "critical": 0,
        "total": 0,
    }
    if finding_name is not None:
        vulnerabilities[finding_name] = {
            "name": finding_name,
            "severity": severity,
            "via": [],
            "nodes": [f"node_modules/{finding_name}"],
        }
        counts[severity] = 1
        counts["total"] = 1
    return {
        "auditReportVersion": 2,
        "vulnerabilities": vulnerabilities,
        "metadata": {"vulnerabilities": counts},
    }


@pytest.mark.parametrize(
    "output",
    (
        "not JSON",
        '{"error":{"summary":"registry unavailable","detail":""}}',
        '{"auditReportVersion":1,"vulnerabilities":{},"metadata":{}}',
        '{"auditReportVersion":2,"vulnerabilities":[],"metadata":{}}',
        '{"auditReportVersion":2,"vulnerabilities":{},"metadata":[]}',
        '{"auditReportVersion":2,"vulnerabilities":{},"metadata":'
        '{"vulnerabilities":[]}}',
    ),
)
def test_malformed_or_failed_npm_audit_is_rejected(output: str) -> None:
    with pytest.raises(AuditPolicyError, match="fixture returned"):
        AUDIT["parse_audit_output"](output, source="fixture")


def test_high_findings_accepts_a_clean_report() -> None:
    assert AUDIT["high_findings"](audit_report()) == []


@pytest.mark.parametrize("severity", ("high", "critical"))
def test_high_findings_returns_blocking_severities(severity: str) -> None:
    report = audit_report("unsafe-package", severity)

    assert AUDIT["high_findings"](report) == [
        ("unsafe-package", report["vulnerabilities"]["unsafe-package"])
    ]


def test_high_findings_allows_moderate_findings() -> None:
    assert AUDIT["high_findings"](audit_report("known-package", "moderate")) == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("finding-shape", "invalid finding"),
        ("finding-name", "invalid finding for package"),
        ("severity", "invalid severity for package"),
        ("high-type", "findings disagree with its metadata"),
        ("high-bool", "findings disagree with its metadata"),
        ("critical-negative", "findings disagree with its metadata"),
        ("count-mismatch", "findings disagree with its metadata"),
    ),
)
def test_high_findings_rejects_invalid_findings_and_metadata(
    mutation: str, message: str
) -> None:
    report = audit_report("package", "high")
    finding = report["vulnerabilities"]["package"]
    counts = report["metadata"]["vulnerabilities"]

    if mutation == "finding-shape":
        report["vulnerabilities"]["package"] = []
    elif mutation == "finding-name":
        finding["name"] = "different-package"
    elif mutation == "severity":
        finding["severity"] = "unknown"
    elif mutation == "high-type":
        counts["high"] = "1"
    elif mutation == "high-bool":
        counts["high"] = True
    elif mutation == "critical-negative":
        counts["critical"] = -1
    elif mutation == "count-mismatch":
        counts["high"] = 0

    with pytest.raises(AuditPolicyError, match=message):
        AUDIT["high_findings"](report)


def package_name(package_dir: Path, root: Path) -> str:
    return package_dir.relative_to(root / "src" / "cyclo").as_posix()


def test_policy_covers_every_shipped_component_lock() -> None:
    component_root = ROOT / "src" / "cyclo" / "components"
    shipped_locks = {
        path.parent.relative_to(ROOT / "src" / "cyclo").as_posix()
        for path in component_root.glob("**/package-lock.json")
        if "node_modules" not in path.parts
    }

    assert tuple(AUDIT["PACKAGES"]) == EXPECTED_PACKAGES
    assert shipped_locks == set(EXPECTED_PACKAGES)


def test_every_shipped_lock_uses_the_same_clean_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audited: list[str] = []

    def clean_audit(package_dir: Path) -> tuple[dict[str, object], int]:
        audited.append(package_name(package_dir, tmp_path))
        return audit_report(), 0

    monkeypatch.setitem(TOOL_GLOBALS, "run_audit", clean_audit)

    AUDIT["audit_repository"](tmp_path)

    assert audited == list(EXPECTED_PACKAGES)
    assert capsys.readouterr().out.splitlines() == [
        f"{name}: no high or critical vulnerabilities"
        for name in EXPECTED_PACKAGES
    ]


@pytest.mark.parametrize("failing_package", EXPECTED_PACKAGES)
@pytest.mark.parametrize("severity", ("high", "critical"))
def test_every_shipped_lock_rejects_high_and_critical_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_package: str,
    severity: str,
) -> None:
    def audit_with_finding(
        package_dir: Path,
    ) -> tuple[dict[str, object], int]:
        name = package_name(package_dir, tmp_path)
        if name == failing_package:
            return audit_report("unsafe-package", severity), 1
        return audit_report(), 0

    monkeypatch.setitem(TOOL_GLOBALS, "run_audit", audit_with_finding)

    with pytest.raises(AuditPolicyError) as raised:
        AUDIT["audit_repository"](tmp_path)

    assert str(raised.value) == (
        f"{failing_package}: high/critical findings: unsafe-package"
    )


@pytest.mark.parametrize("failing_package", EXPECTED_PACKAGES)
def test_every_shipped_lock_rejects_an_unexpected_audit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_package: str,
) -> None:
    def audit_with_failure(
        package_dir: Path,
    ) -> tuple[dict[str, object], int]:
        name = package_name(package_dir, tmp_path)
        return audit_report(), 1 if name == failing_package else 0

    monkeypatch.setitem(TOOL_GLOBALS, "run_audit", audit_with_failure)

    with pytest.raises(AuditPolicyError) as raised:
        AUDIT["audit_repository"](tmp_path)

    assert str(raised.value) == (
        f"{failing_package}: npm audit exited with status 1 "
        "without high/critical findings"
    )


def test_run_audit_rejects_an_unexpected_process_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = SimpleNamespace(stdout=json.dumps(audit_report()), returncode=2)
    monkeypatch.setattr(AUDIT["subprocess"], "run", lambda *args, **kwargs: completed)

    with pytest.raises(AuditPolicyError) as raised:
        AUDIT["run_audit"](tmp_path / "component")

    assert str(raised.value) == (
        f"npm audit ({tmp_path / 'component'}) failed with status 2"
    )
