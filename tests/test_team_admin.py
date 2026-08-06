from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.errors import CycloError
from cyclo.state import StateStore
from cyclo.team.admin import TaskAdmin, _bind, read_task_specification


class FakeImages:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def command(self, arguments, **options):
        self.calls.append((list(arguments), dict(options)))
        if arguments[:3] == ["container", "ls", "--all"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_task_tool_runs_with_only_queue_and_spec_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: "unix:///var/run/docker.sock",
    )
    store = StateStore(tmp_path / "state")
    store._docker_endpoint = "unix:///var/run/docker.sock"
    tasks = store.tasks_dir("demo")
    jobs = store.jobs_dir("demo")
    tasks.mkdir(parents=True)
    jobs.mkdir()
    specification = b"Build a UART.\n"
    instance = SimpleNamespace(
        id="demo",
        image="sha256:" + "a" * 64,
    )
    admin = TaskAdmin(store, instance)
    images = FakeImages()
    admin.images = images

    assert (
        admin.run(
            "task-create",
            ("uart",),
            specification=specification,
        )
        == 0
    )

    command, options = images.calls[-1]
    assert command[:2] == ["run", "--rm"]
    assert "--read-only" in command
    assert ["--user", f"{os.getuid()}:{os.getgid()}"] == command[
        command.index("--user") : command.index("--user") + 2
    ]
    assert ["--network", "none"] == command[
        command.index("--network") : command.index("--network") + 2
    ]
    assert ["--env", "CYCLO_ADMIN_TOOL=1"] == command[
        command.index("--env") : command.index("--env") + 2
    ]
    assert str(tasks.resolve()) in "\n".join(command)
    assert str(jobs.resolve()) in "\n".join(command)
    specification_mount = next(
        item for item in command if "dst=/run/cyclo/queue-spec.md" in item
    )
    assert str(store.root.resolve()) in specification_mount
    assert str(tmp_path / "task.md") not in "\n".join(command)
    assert command[-3:] == [
        "/agentws/bin/task-create",
        "uart",
        "/run/cyclo/queue-spec.md",
    ]
    assert options == {"check": False, "capture": False}
    staged_source = specification_mount.split("src=", 1)[1].split(",", 1)[0]
    assert not Path(staged_source).exists()


def test_task_tool_mount_quotes_docker_csv_paths(tmp_path: Path) -> None:
    source = tmp_path / 'queue,"one'
    source.mkdir()

    rendered = _bind(source, "/agentws/tasks")

    assert 'src="' in rendered
    assert 'queue,""one' in rendered
    assert rendered.endswith("dst=/agentws/tasks")


@pytest.mark.parametrize(
    ("tool", "tasks_read_only", "has_jobs"),
    [
        ("task-list", True, False),
        ("task-show", True, False),
        ("task-comment", False, False),
        ("task-state", False, False),
    ],
)
def test_task_tool_receives_only_its_required_queue_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    tasks_read_only: bool,
    has_jobs: bool,
) -> None:
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: "unix:///var/run/docker.sock",
    )
    store = StateStore(tmp_path / "state")
    store._docker_endpoint = "unix:///var/run/docker.sock"
    tasks = store.tasks_dir("demo")
    jobs = store.jobs_dir("demo")
    tasks.mkdir(parents=True)
    jobs.mkdir()
    admin = TaskAdmin(
        store,
        SimpleNamespace(id="demo", image="sha256:" + "a" * 64),
    )
    images = FakeImages()
    admin.images = images

    assert admin.run(tool) == 0

    command, _options = images.calls[-1]
    mounts = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--mount"
    ]
    assert len(mounts) == 1 + int(has_jobs)
    tasks_mount = next(value for value in mounts if "dst=/agentws/tasks" in value)
    assert tasks_mount.endswith(",readonly") is tasks_read_only
    assert any("dst=/agentws/jobs" in value for value in mounts) is has_jobs


def test_job_create_receives_read_only_task_and_writable_job_queues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: "unix:///var/run/docker.sock",
    )
    store = StateStore(tmp_path / "state")
    store._docker_endpoint = "unix:///var/run/docker.sock"
    store.tasks_dir("demo").mkdir(parents=True)
    store.jobs_dir("demo").mkdir()
    admin = TaskAdmin(
        store,
        SimpleNamespace(id="demo", image="sha256:" + "a" * 64),
    )
    images = FakeImages()
    admin.images = images

    assert admin.run(
        "job-create",
        ("pcie-rtl-r4", "--role", "rtl", "--task-id", "pcie"),
        specification=b"Repair the link.\n",
    ) == 0

    command, _options = images.calls[-1]
    task_mount = next(
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--mount" and "dst=/agentws/tasks" in command[index + 1]
    )
    job_mount = next(
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--mount" and "dst=/agentws/jobs" in command[index + 1]
    )
    assert task_mount.endswith(",readonly")
    assert not job_mount.endswith(",readonly")
    assert command[-7:] == [
        "/agentws/bin/job-create",
        "pcie-rtl-r4",
        "--role",
        "rtl",
        "--task-id",
        "pcie",
        "/run/cyclo/queue-spec.md",
    ]


def test_task_tool_rejects_unlisted_agentws_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: "unix:///var/run/docker.sock",
    )
    store = StateStore(tmp_path / "state")
    store._docker_endpoint = "unix:///var/run/docker.sock"
    admin = TaskAdmin(
        store,
        SimpleNamespace(id="demo", image="sha256:" + "a" * 64),
    )

    with pytest.raises(CycloError, match="invalid AgentWS task tool"):
        admin.run("job-claim")


def test_task_tool_refuses_host_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cyclo.team.admin.os.getuid", lambda: 0)
    store = StateStore(tmp_path / "state")
    admin = TaskAdmin(
        store,
        SimpleNamespace(id="demo", image="sha256:" + "a" * 64),
    )

    with pytest.raises(CycloError, match="refuses host root"):
        admin.run("task-list")


def test_task_specification_read_is_bounded_and_rejects_symlinked_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    specification = source / "task.md"
    specification.write_bytes(b"Build a UART.\n")

    assert read_task_specification(specification) == b"Build a UART.\n"

    target = tmp_path / "private"
    target.write_bytes(b"not project data")
    specification.unlink()
    specification.symlink_to(target)
    with pytest.raises(CycloError, match="only real directories"):
        read_task_specification(specification)

    specification.unlink()
    nested = source / "nested"
    nested.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CycloError, match="only real directories"):
        read_task_specification(nested / "private")


def test_task_specification_read_rejects_oversized_and_nonregular_input(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(CycloError, match="exceeds the .*byte limit"):
        read_task_specification(oversized)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(CycloError, match="not a regular file"):
        read_task_specification(directory)
