from __future__ import annotations

import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from cyclo.team.resources import packaged_agentws_runtime


def runtime_copy(tmp_path: Path) -> Path:
    runtime = Path(
        shutil.copytree(
            packaged_agentws_runtime(),
            tmp_path / "runtime",
            copy_function=shutil.copy2,
        )
    )
    subprocess.run(
        [str(runtime / "bin" / "job-init")],
        check=True,
        capture_output=True,
    )
    return runtime


def make_task(
    runtime: Path,
    task_id: str,
    *,
    state: str = "open\n",
    spec: bytes = b"# Spec\n\nBuild it.\n",
    log: bytes = b"## Log entry\n\nStarted.\n",
    result: bytes | None = None,
) -> Path:
    task = runtime / "tasks" / task_id
    task.mkdir()
    (task / "state").write_text(state, encoding="utf-8")
    (task / "spec.md").write_bytes(spec)
    (task / "log.md").write_bytes(log)
    if result is not None:
        (task / "result.md").write_bytes(result)
    return task


def run_task_tool(
    runtime: Path, name: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(runtime / "bin" / name), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )


def test_task_read_commands_preserve_normal_output(tmp_path: Path) -> None:
    runtime = runtime_copy(tmp_path)
    make_task(runtime, "beta", state="done\n")
    make_task(
        runtime,
        "alpha",
        result=b"# Result\n\nComplete.\n",
    )

    listed = run_task_tool(runtime, "task-list")
    assert listed.returncode == 0
    assert listed.stdout == "alpha\topen\nbeta\tdone\n"
    assert listed.stderr == ""

    shown = run_task_tool(runtime, "task-show", "alpha")
    assert shown.returncode == 0
    assert shown.stdout == (
        "# Task alpha\n"
        "\n"
        "State: open\n"
        "\n"
        "## Spec\n"
        "# Spec\n\nBuild it.\n"
        "\n"
        "## Log\n"
        "## Log entry\n\nStarted.\n"
        "\n"
        "## Result\n"
        "# Result\n\nComplete.\n"
    )
    assert shown.stderr == ""


@pytest.mark.parametrize("entry_kind", ("symlink", "fifo"))
def test_task_show_never_reads_non_regular_content(
    tmp_path: Path, entry_kind: str
) -> None:
    runtime = runtime_copy(tmp_path)
    task = make_task(runtime, "unsafe")
    outside = tmp_path / "outside"
    outside.write_text("EXTERNAL-SECRET\n", encoding="utf-8")
    (task / "spec.md").unlink()
    if entry_kind == "symlink":
        (task / "spec.md").symlink_to(outside)
    else:
        os.mkfifo(task / "spec.md")

    shown = run_task_tool(runtime, "task-show", "unsafe")
    assert shown.returncode != 0
    assert "EXTERNAL-SECRET" not in shown.stdout
    assert "EXTERNAL-SECRET" not in shown.stderr


def test_task_directories_and_state_are_link_safe_and_validated(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state").write_text("done\nSECRET\n", encoding="utf-8")
    (runtime / "tasks" / "linked").symlink_to(
        outside, target_is_directory=True
    )
    task = make_task(runtime, "real")
    (task / "state").unlink()
    (task / "state").symlink_to(outside / "state")

    listed = run_task_tool(runtime, "task-list")
    assert listed.returncode != 0
    assert "SECRET" not in listed.stdout
    assert "SECRET" not in listed.stderr
    assert "\x1b" not in listed.stderr

    shown = run_task_tool(runtime, "task-show", "linked")
    assert shown.returncode != 0
    assert "does not exist" in shown.stderr
    assert "SECRET" not in shown.stdout
    assert "SECRET" not in shown.stderr


def test_task_show_bounds_reads_and_escapes_terminal_controls(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    controls = (
        b"line\tcolumn\n"
        b"\x1b[31mred\x1b[0m\rback\bspace\x00nul\xc2\x9bclear"
    )
    make_task(
        runtime,
        "hostile",
        spec=controls + b"s" * (160 * 1024),
        log=b"l" * (512 * 1024 + 1),
        result=b"r" * (160 * 1024 + 1),
    )

    shown = run_task_tool(runtime, "task-show", "hostile")
    assert shown.returncode == 0
    assert "\x1b" not in shown.stdout
    assert "\x00" not in shown.stdout
    assert "\\x1b[31mred\\x1b[0m\\x0dback\\x08space\\x00nul\\x9bclear" in shown.stdout
    assert "line\tcolumn\n" in shown.stdout
    assert shown.stdout.count("[... content truncated ...]") == 3
    assert len(shown.stdout) < 900 * 1024


def test_task_ids_states_and_directory_enumeration_are_bounded(
    tmp_path: Path,
) -> None:
    runtime = runtime_copy(tmp_path)
    make_task(runtime, "invalid-state", state="open\x1b[2J\n")

    invalid_id = run_task_tool(runtime, "task-show", "../escape")
    assert invalid_id.returncode != 0
    assert "invalid task ID" in invalid_id.stderr

    invalid_state = run_task_tool(runtime, "task-list")
    assert invalid_state.returncode != 0
    assert "\x1b" not in invalid_state.stderr
    assert "\\x1b[2J" in invalid_state.stderr

    namespace = runpy.run_path(
        str(runtime / "tools" / "queue_reader.py")
    )
    reader_type = namespace["ConfinedQueueReader"]
    limit_error = namespace["QueueLimitError"]
    limited = tmp_path / "limited"
    limited.mkdir()
    for name in ("one", "two", "three"):
        (limited / name).mkdir()
    with reader_type(limited, max_entries=2) as reader:
        with pytest.raises(limit_error, match="exceeds 2 entries"):
            reader.list_directories(())

    bounded = tmp_path / "bounded"
    bounded.mkdir()
    (bounded / "one").write_text("1234", encoding="utf-8")
    (bounded / "two").write_text("5", encoding="utf-8")
    with reader_type(bounded, max_read_bytes=4) as reader:
        assert reader.read_file(("one",), max_bytes=4)[0] == "1234"
        with pytest.raises(limit_error, match="exceed.*4 bytes"):
            reader.read_file(("two",), max_bytes=1)
