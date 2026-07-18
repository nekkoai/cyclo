from __future__ import annotations

import gzip
import io
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NORMALIZER = ROOT / "tools" / "normalize-distributions"
EPOCH = 1700000000


def write_sdist(path: Path, *, mtime: int, reverse: bool) -> None:
    members = [
        ("cyclo_agent-0.2.0/cyclo/runner.py", b"print('cyclo')\n", 0o755),
        ("cyclo_agent-0.2.0/README.md", b"# Cyclo\n", 0o644),
    ]
    if reverse:
        members.reverse()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, contents, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = mode
            info.mtime = mtime
            info.uid = 1000
            info.gid = 1000
            info.uname = "builder"
            info.gname = "builder"
            archive.addfile(info, io.BytesIO(contents))
    with path.open("wb") as output, gzip.GzipFile(
        filename="source-tree",
        mode="wb",
        fileobj=output,
        mtime=mtime,
    ) as archive:
        archive.write(payload.getvalue())


def write_wheel(
    path: Path,
    *,
    timestamp: tuple[int, int, int, int, int, int],
    reverse: bool,
) -> None:
    members = [
        ("cyclo/runner.py", b"print('cyclo')\n", 0o755),
        (
            "cyclo_agent-0.2.0.dist-info/METADATA",
            b"Name: cyclo-agent\nVersion: 0.2.0\n",
            0o644,
        ),
    ]
    if reverse:
        members.reverse()
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents, mode in members:
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED)


def test_normalizer_makes_archives_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    wheel_name = "cyclo_agent-0.2.0-py3-none-any.whl"
    sdist_name = "cyclo_agent-0.2.0.tar.gz"

    write_wheel(first / wheel_name, timestamp=(2025, 1, 2, 3, 4, 6), reverse=False)
    write_wheel(second / wheel_name, timestamp=(2026, 7, 8, 9, 10, 12), reverse=True)
    write_sdist(first / sdist_name, mtime=1735787046, reverse=False)
    write_sdist(second / sdist_name, mtime=1783501812, reverse=True)

    env = {**os.environ, "SOURCE_DATE_EPOCH": str(EPOCH)}
    for directory in (first, second):
        subprocess.run(
            [sys.executable, str(NORMALIZER), str(directory)],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

    assert (first / wheel_name).read_bytes() == (second / wheel_name).read_bytes()
    assert (first / sdist_name).read_bytes() == (second / sdist_name).read_bytes()
    normalized = {
        wheel_name: (first / wheel_name).read_bytes(),
        sdist_name: (first / sdist_name).read_bytes(),
    }
    subprocess.run(
        [sys.executable, str(NORMALIZER), str(first)],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    assert {name: (first / name).read_bytes() for name in normalized} == normalized

    value = datetime.fromtimestamp(EPOCH, timezone.utc)
    expected_zip_time = (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second // 2 * 2,
    )
    with zipfile.ZipFile(first / wheel_name) as archive:
        assert all(info.date_time == expected_zip_time for info in archive.infolist())
        runner = archive.getinfo("cyclo/runner.py")
        assert stat.S_IMODE(runner.external_attr >> 16) == 0o755

    with tarfile.open(first / sdist_name) as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(
            member.name for member in members
        )
        assert all(member.mtime == EPOCH for member in members)
        assert all(
            (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
            for member in members
        )
        runner = archive.getmember("cyclo_agent-0.2.0/cyclo/runner.py")
        assert runner.mode == 0o755
