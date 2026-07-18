from __future__ import annotations

import runpy
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "tools" / "secret-scan"
SCANNER_API = runpy.run_path(str(SCANNER))


def run_scanner(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), *(str(path) for path in paths)],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_secret_scan_accepts_repository_history_and_worktree() -> None:
    result = run_scanner()
    assert result.returncode == 0, result.stdout
    assert "secret scan: PASS" in result.stdout


def test_worktree_scan_ignores_a_tracked_path_deleted_from_disk(tmp_path: Path) -> None:
    missing = tmp_path / "deleted-tracked-file"
    assert SCANNER_API["scan_paths"]([missing]) == []


def test_secret_scan_rejects_a_private_key_in_an_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "leaked.txt"
    marker = "-----BEGIN " + "PRIVATE KEY-----"
    artifact.write_text(marker + "\nnot-a-real-key\n", encoding="utf-8")

    result = run_scanner(artifact)
    assert result.returncode == 1
    assert "private key" in result.stdout


def test_secret_scan_rejects_an_encrypted_pkcs8_key(tmp_path: Path) -> None:
    artifact = tmp_path / "encrypted.pem"
    marker = "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----"
    artifact.write_text(marker + "\nnot-a-real-key\n", encoding="utf-8")

    result = run_scanner(artifact)

    assert result.returncode == 1
    assert "private key" in result.stdout


def test_secret_scan_checks_large_binary_files_instead_of_skipping_them(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "large-binary.dat"
    marker = b"AKIA" + b"A" * 16
    with artifact.open("wb") as handle:
        handle.write(b"\0")
        handle.seek(20 * 1024 * 1024 + 127)
        handle.write(marker)

    result = run_scanner(artifact)

    assert result.returncode == 1
    assert "AWS access key" in result.stdout


def test_archive_members_are_scanned_as_streams(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "artifact.zip"
    marker = b"AKIA" + b"B" * 16
    chunk_size = SCANNER_API["SCAN_CHUNK_BYTES"]
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "binary.dat", b"\0" + b"x" * (chunk_size - 7) + b"\0" + marker
        )

    def reject_unbounded_read(*_args, **_kwargs):
        raise AssertionError("ZipFile.read() would materialize the complete member")

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_unbounded_read)
    problems = SCANNER_API["scan_archive"](artifact)

    assert any("AWS access key" in problem for problem in problems)
