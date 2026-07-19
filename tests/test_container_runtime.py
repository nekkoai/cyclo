from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from cyclo import container_runtime


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
    monkeypatch.setenv("CYCLO_AGENTWS_RUNTIME", str(runtime))
    monkeypatch.setenv("AGENTWS_TEAM_ROSTER", str(roster))
    monkeypatch.delenv("CYCLO_VERBOSE", raising=False)
    (runtime / "agents").mkdir(parents=True)


def test_queue_recovery_precedes_every_runtime_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "agentws"
    roster = tmp_path / "team"
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

    assert container_runtime.main() == 42
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
    roster = tmp_path / "team"
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

    assert container_runtime.main() == 70
    assert events == ["recover", "terminate:0"]


def test_stop_during_recovery_starts_no_runtime_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "agentws"
    roster = tmp_path / "team"
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

    assert container_runtime.main() == 0
    assert events == ["recover", "terminate:1"]
