from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from cyclo.components.team import runtime as container_runtime


CONTAINER_PROJECT_CONFIG = (
    "name runtime-test\n"
    "description Container supervisor test.\n"
    "team /team ro\n"
    "mount source /workspace/source rw\n"
    "mount references /readonly/references ro\n"
)


def test_runtime_lock_excludes_a_second_queue_owner(tmp_path: Path) -> None:
    runtime = tmp_path / "agentws"
    runtime.mkdir()

    first = container_runtime.acquire_runtime_lock(runtime)
    try:
        with pytest.raises(BlockingIOError):
            container_runtime.acquire_runtime_lock(runtime)
    finally:
        os.close(first)


def runtime_environment(
    monkeypatch: pytest.MonkeyPatch, runtime: Path, roster: Path
) -> None:
    monkeypatch.setattr(container_runtime, "AGENTWS_ROOT", runtime)
    monkeypatch.setattr(container_runtime, "TEAM_ROOT", roster.parent)
    monkeypatch.setattr(
        container_runtime,
        "WORKSPACE_ROOT",
        runtime.parent / "workspace",
    )
    monkeypatch.setattr(
        container_runtime,
        "PI_AGENT_ROOT",
        runtime.parent / "pi" / "agent",
    )
    (runtime / "agents").mkdir(parents=True)
    (runtime / "project.cyclo").write_text(
        CONTAINER_PROJECT_CONFIG,
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "empty",
        "whitespace",
        "invalid-utf8",
        "oversized",
        "directory",
        "fifo",
        "symlink",
    ],
)
def test_invalid_project_config_stops_before_runtime_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    runtime = tmp_path / "agentws"
    roster = tmp_path / "team-root" / "team"
    roster.parent.mkdir()
    runtime_environment(monkeypatch, runtime, roster)
    project_config = runtime / "project.cyclo"
    project_config.unlink()
    if kind == "empty":
        project_config.touch()
    elif kind == "whitespace":
        project_config.write_text(" \n", encoding="utf-8")
    elif kind == "invalid-utf8":
        project_config.write_bytes(b"\xff")
    elif kind == "oversized":
        project_config.write_bytes(
            b"x" * (container_runtime.MAX_PROJECT_CONFIG_BYTES + 1)
        )
    elif kind == "directory":
        project_config.mkdir()
    elif kind == "fifo":
        os.mkfifo(project_config)
    elif kind == "symlink":
        target = tmp_path / "project-target.cyclo"
        target.write_text(CONTAINER_PROJECT_CONFIG, encoding="utf-8")
        project_config.symlink_to(target)

    monkeypatch.setattr(
        container_runtime.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("runtime process was started"),
    )

    assert container_runtime.main(["--roster", str(roster)]) == 70
    assert "invalid project config" in capsys.readouterr().err


def test_queue_recovery_precedes_every_runtime_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "agentws"
    roster = tmp_path / "team-root" / "team"
    roster.parent.mkdir()
    runtime_environment(monkeypatch, runtime, roster)
    events: list[tuple[str, object]] = []

    class Process:
        def __init__(self, label: str, status: int | None) -> None:
            self.label = label
            self.status = status

        def poll(self):
            return self.status

        def wait(self):
            assert self.status is not None
            return self.status

    def popen(command, **kwargs):
        if command[0].endswith("job-reset-orphans"):
            assert kwargs["start_new_session"] is True
            assert kwargs["pass_fds"]
            assert kwargs["env"]["CYCLO_RUNTIME_LOCK_FD"] == str(
                kwargs["pass_fds"][0]
            )
            events.append(("recover", tuple(command)))
            return Process("recover", 0)
        label = "team" if command[0].endswith("run_agentws") else "viewer"
        events.append(("start", label))
        return Process(label, 42 if label == "team" else None)

    monkeypatch.setattr(container_runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(
        container_runtime,
        "terminate",
        lambda processes: events.append(("terminate", len(processes))),
    )

    assert container_runtime.main(["--roster", str(roster)]) == 42
    assert events == [
        (
            "recover",
            (
                str(runtime / "bin" / "job-reset-orphans"),
                "--all-active",
            ),
        ),
        ("start", "viewer"),
        ("start", "team"),
        ("terminate", 2),
    ]


def test_failed_queue_recovery_starts_no_runtime_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "agentws"
    roster = tmp_path / "team-root" / "team"
    roster.parent.mkdir()
    runtime_environment(monkeypatch, runtime, roster)
    events: list[str] = []

    class FailedRecovery:
        def poll(self):
            return 70

        def wait(self):
            return 70

    def popen(command, **_kwargs):
        assert command[0].endswith("job-reset-orphans")
        events.append("recover")
        return FailedRecovery()

    monkeypatch.setattr(container_runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(
        container_runtime,
        "terminate",
        lambda processes: events.append(f"terminate:{len(processes)}"),
    )

    assert container_runtime.main(["--roster", str(roster)]) == 70
    assert events == ["recover", "terminate:0"]


def test_stop_during_recovery_starts_no_runtime_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "agentws"
    roster = tmp_path / "team-root" / "team"
    roster.parent.mkdir()
    runtime_environment(monkeypatch, runtime, roster)
    events: list[str] = []

    class BlockingRecovery:
        pid = 12345

        def poll(self):
            return None

    def popen(command, **_kwargs):
        assert command[0].endswith("job-reset-orphans")
        events.append("recover")
        return BlockingRecovery()

    def request_stop(_seconds: float) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(container_runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(container_runtime.time, "sleep", request_stop)
    monkeypatch.setattr(
        container_runtime,
        "terminate",
        lambda processes: events.append(f"terminate:{len(processes)}"),
    )

    assert container_runtime.main(["--roster", str(roster)]) == 0
    assert events == ["recover", "terminate:1"]
