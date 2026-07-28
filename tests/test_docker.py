from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cyclo.docker import (
    Docker,
    DockerContainerState,
    ContainerSpec,
    container_command,
    docker_socket_paths,
    validate_mount_collection,
    validate_mount_boundaries,
    validate_container_spec,
    mount,
)
from cyclo.errors import CycloError
from cyclo.project import ProjectMount
from cyclo.state import Instance
from cyclo.team import load_team


SYSTEM = "0123456789ab"


def instance(team: Path, project: Path) -> Instance:
    provider_socket_dir = project.parent / ".cyclo-provider"
    provider_socket_dir.mkdir(exist_ok=True)
    return Instance(
        id="review-team-123",
        team_name="review-team",
        team_path=str(team),
        project_path=str(project),
        generation="deadbeef-dirty-a1",
        providers=["anthropic", "openai-codex"],
        models=["anthropic/claude-test", "openai-codex/gpt-test"],
        container_name=f"cyclo-{SYSTEM}-team-review-team-123",
        network_name=f"cyclo-{SYSTEM}-team-review-team-123-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=True,
        launch_id="0" * 32,
        provider_socket_path=str(provider_socket_dir / "component.sock"),
        provider_generation="provider-generation",
    )


def owned_container_info(
    selected: Instance,
    *,
    system: str = SYSTEM,
    container_id: str = "verified-container-id",
    launch_id: str | None = None,
    running: bool = True,
) -> dict[str, object]:
    labels = {
        "io.cyclo.system": system,
        "io.cyclo.kind": "team",
        "io.cyclo.instance": selected.id,
    }
    selected_launch = selected.launch_id if launch_id is None else launch_id
    if selected_launch:
        labels["cyclo.launch"] = selected_launch
    return {
        "Id": container_id,
        "Config": {"Labels": labels},
        "State": {"Running": running},
    }


def test_launch_validation_requires_an_existing_provider_socket_directory(
    tmp_path: Path, team_repo: Path
) -> None:
    selected_instance = instance(team_repo, tmp_path)
    provider_dir = Path(selected_instance.provider_socket_path).parent
    provider_dir.rmdir()
    spec = ContainerSpec(
        instance=selected_instance,
        team=load_team(team_repo),
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        tasks_dir=tmp_path / "tasks",
        jobs_dir=tmp_path / "jobs",
        agents_dir=tmp_path / "agents",
        pi_root=tmp_path / "pi",
        provider_socket_dir=provider_dir,
        system=SYSTEM,
        port=0,
    )

    with pytest.raises(CycloError, match="invalid Cyclo provider socket directory"):
        validate_container_spec(spec)

    assert "docker" in container_command(spec)


def test_container_argv_has_only_scoped_runtime_mounts(
    tmp_path: Path, team_repo: Path, project_repo: Path, monkeypatch
) -> None:
    retry_values = {
        "AGENTWS_MAX_JOB_ATTEMPTS": "4",
        "AGENTWS_MAX_CONSECUTIVE_FAILURES": "6",
        "AGENTWS_RETRY_INITIAL_SECONDS": "3",
        "AGENTWS_RETRY_MAX_SECONDS": "20",
    }
    for name, value in retry_values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENTWS_UNSAFE_UNDOCUMENTED", "must-not-pass")
    run = tmp_path / "state" / "agentws"
    pi = tmp_path / "state" / "pi"
    queue = tmp_path / "state" / "queue"
    run.mkdir(parents=True)
    pi.mkdir()
    for name in ("tasks", "jobs", "agents"):
        (queue / name).mkdir(parents=True, exist_ok=True)
    selected_instance = instance(team_repo, project_repo)
    selected_instance.launch_id = "launch-identity"
    spec = ContainerSpec(
        instance=selected_instance,
        team=load_team(team_repo),
        project=project_repo,
        runtime_root=run,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
        provider_socket_dir=Path(selected_instance.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )

    command = container_command(spec)
    rendered = " ".join(command)

    assert command[:3] == ["docker", "run", "--detach"]
    assert "--publish" not in command
    assert "AGENTWS_TEAM_ROSTER=/team/team" in command
    assert "AGENTWS_SYSTEM_PROTOCOL=/agentws/AGENTS.md" in command
    assert "AGENTWS_TEAM_PROTOCOL=/team/AGENTS.md" in command
    assert "AGENTWS_WORKSPACE=/workspace" in command
    assert "CYCLO_PROVIDER_RUNTIME_HEALTH_URL" not in command
    assert "CYCLO_PROVIDER_SOCKET=/run/cyclo/provider/component.sock" in command
    assert "CYCLO_PROVIDER_TOKEN" not in rendered
    assert "CYCLO_PROVIDER_BASE_URL" not in rendered
    assert f"type=bind,src={team_repo},dst=/team,readonly" in command
    assert f"type=bind,src={project_repo},dst=/workspace" in command
    assert f"type=bind,src={project_repo},dst=/workspace,readonly" not in command
    assert f"type=bind,src={run},dst=/agentws,readonly" in command
    assert f"type=bind,src={pi},dst=/home/cyclo/.pi" in command
    assert f"type=bind,src={pi},dst=/home/cyclo/.pi,readonly" not in command
    assert (
        f"type=bind,src={project_repo.parent / '.cyclo-provider'},"
        "dst=/run/cyclo/provider,readonly"
    ) in command
    assert f"type=bind,src={queue / 'tasks'},dst=/agentws/tasks" in command
    assert "gateway-token" not in rendered
    assert "/var/run/docker.sock" not in rendered
    assert str(Path.home() / ".pi" / "agent") not in rendered
    assert "--security-opt" in command
    assert command[command.index("--cap-drop") + 1] == "NET_RAW"
    assert "cyclo.launch=launch-identity" in command
    assert f"io.cyclo.system={SYSTEM}" in command
    assert "io.cyclo.kind=team" in command
    assert "io.cyclo.instance=review-team-123" in command
    assert "cyclo.instance=review-team-123" not in command
    for name, value in retry_values.items():
        assert f"{name}={value}" in command
    assert "AGENTWS_UNSAFE_UNDOCUMENTED" not in rendered


def test_container_argv_preserves_host_supplementary_groups(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 1200)
    monkeypatch.setattr(os, "getgid", lambda: 120)
    monkeypatch.setattr(os, "getgroups", lambda: [900, 120, 42, 900, 42])
    selected_instance = instance(team_repo, project_repo)
    spec = ContainerSpec(
        instance=selected_instance,
        team=load_team(team_repo),
        project=project_repo,
        runtime_root=tmp_path / "runtime",
        tasks_dir=tmp_path / "tasks",
        jobs_dir=tmp_path / "jobs",
        agents_dir=tmp_path / "agents",
        pi_root=tmp_path / "pi",
        provider_socket_dir=Path(selected_instance.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )

    command = container_command(spec)

    assert "CYCLO_HOST_UID=1200" in command
    assert "CYCLO_HOST_GID=120" in command
    assert command.count("CYCLO_EXTRA_GROUPS=42:900") == 1


def test_legacy_project_mount_is_always_writable(
    tmp_path: Path, team_repo: Path, project_repo: Path
) -> None:
    runtime = tmp_path / "agentws"
    pi = tmp_path / "pi"
    queue = tmp_path / "queue"
    runtime.mkdir()
    pi.mkdir()
    for name in ("tasks", "jobs", "agents"):
        (queue / name).mkdir(parents=True)
    selected_instance = instance(team_repo, project_repo)
    spec = ContainerSpec(
        instance=selected_instance,
        team=load_team(team_repo),
        project=project_repo,
        runtime_root=runtime,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
        provider_socket_dir=Path(selected_instance.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )

    command = container_command(spec)

    assert f"type=bind,src={project_repo},dst=/workspace" in command
    assert f"type=bind,src={project_repo},dst=/workspace,readonly" not in command


def test_named_project_mounts_use_read_only_namespace_and_explicit_modes(
    tmp_path: Path, team_repo: Path
) -> None:
    runtime = tmp_path / "agentws"
    pi = tmp_path / "pi"
    queue = tmp_path / "queue"
    layout = tmp_path / "layout"
    readonly_layout = tmp_path / "readonly-layout"
    source = tmp_path / "source"
    docs = tmp_path / "docs"
    for path in (runtime, pi, layout, readonly_layout, source, docs):
        path.mkdir()
    (layout / "source").mkdir()
    (readonly_layout / "docs").mkdir()
    for name in ("tasks", "jobs", "agents"):
        (queue / name).mkdir(parents=True)
    mounts = (
        ProjectMount("source", source, "rw", 4),
        ProjectMount("docs", docs, "ro", 5),
    )
    selected_instance = instance(team_repo, tmp_path)
    spec = ContainerSpec(
        instance=selected_instance,
        team=load_team(team_repo),
        project=tmp_path,
        runtime_root=runtime,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
        provider_socket_dir=Path(selected_instance.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
        project_mounts=mounts,
        workspace_layout=layout,
        readonly_layout=readonly_layout,
    )

    command = container_command(spec)

    assert f"type=bind,src={layout},dst=/workspace,readonly" in command
    assert f"type=bind,src={readonly_layout},dst=/readonly,readonly" in command
    assert f"type=bind,src={source},dst=/workspace/source" in command
    assert f"type=bind,src={source},dst=/workspace/source,readonly" not in command
    assert f"type=bind,src={docs},dst=/readonly/docs,readonly" in command
    assert not any(
        value.startswith("CYCLO_PROJECT_MANIFEST=") for value in command
    )
    layout_index = command.index(f"type=bind,src={layout},dst=/workspace,readonly")
    source_index = command.index(f"type=bind,src={source},dst=/workspace/source")
    readonly_index = command.index(
        f"type=bind,src={readonly_layout},dst=/readonly,readonly"
    )
    docs_index = command.index(f"type=bind,src={docs},dst=/readonly/docs,readonly")
    assert layout_index < readonly_index < source_index < docs_index


def test_docker_mount_rejects_unknown_access_modes(tmp_path: Path) -> None:
    with pytest.raises(CycloError, match="invalid Docker bind mode"):
        mount(tmp_path, Path("/workspace/source"), "read-mostly")


def test_container_command_rejects_path_like_named_mount(
    tmp_path: Path, team_repo: Path
) -> None:
    runtime = tmp_path / "agentws"
    pi = tmp_path / "pi"
    queue = tmp_path / "queue"
    layout = tmp_path / "layout"
    readonly_layout = tmp_path / "readonly-layout"
    source = tmp_path / "source"
    for path in (runtime, pi, layout, readonly_layout, source):
        path.mkdir()
    for name in ("tasks", "jobs", "agents"):
        (queue / name).mkdir(parents=True)
    selected_instance = instance(team_repo, tmp_path)
    spec = ContainerSpec(
        instance=selected_instance,
        team=load_team(team_repo),
        project=tmp_path,
        runtime_root=runtime,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
        provider_socket_dir=Path(selected_instance.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
        project_mounts=(ProjectMount("..", source, "rw", 1),),
        workspace_layout=layout,
        readonly_layout=readonly_layout,
    )

    with pytest.raises(CycloError, match="invalid named mount target"):
        container_command(spec)


def test_named_mount_collection_rejects_cross_team_and_project_overlap(
    tmp_path: Path,
) -> None:
    team = tmp_path / "team"
    nested_project = team / "source"
    team.mkdir()
    nested_project.mkdir()

    with pytest.raises(CycloError, match="separate filesystem trees"):
        validate_mount_collection(
            ((team, "team 'one'"),),
            ((nested_project, "project mount 'source'"),),
            tmp_path / "state",
            tmp_path / "pi",
        )


def test_team_and_project_must_not_overlap(tmp_path: Path) -> None:
    team = tmp_path / "repo"
    project = team / "project"
    project.mkdir(parents=True)

    with pytest.raises(CycloError, match="separate filesystem trees"):
        validate_mount_boundaries(team, project, tmp_path / "state", tmp_path / "pi")


def test_mounts_must_not_cover_host_credentials(tmp_path: Path) -> None:
    team = tmp_path / "team"
    project = tmp_path / "home"
    credentials = project / ".pi" / "agent"
    team.mkdir()
    credentials.mkdir(parents=True)

    with pytest.raises(CycloError, match="credential"):
        validate_mount_boundaries(team, project, tmp_path / "state", credentials)


def test_rootless_and_configured_docker_sockets_are_protected(
    tmp_path: Path, monkeypatch
) -> None:
    team = tmp_path / "team"
    project = tmp_path / "project"
    team.mkdir()
    project.mkdir()
    socket = project / "runtime" / "docker.sock"
    monkeypatch.setenv("DOCKER_HOST", f"unix://{socket}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))

    assert socket.resolve() in docker_socket_paths()
    assert (tmp_path / "xdg-runtime" / "docker.sock").resolve() in docker_socket_paths()
    with pytest.raises(CycloError, match="Docker socket"):
        validate_mount_boundaries(
            team,
            project,
            tmp_path / "state",
            tmp_path / "host-pi",
        )


@pytest.mark.parametrize("source", [Path("/proc/self"), Path("/sys"), Path("/dev"), Path("/run")])
def test_host_pseudo_filesystems_cannot_be_mounted(source: Path, tmp_path: Path) -> None:
    team = tmp_path / "team"
    team.mkdir()

    with pytest.raises(CycloError, match="host .* filesystem"):
        validate_mount_boundaries(
            team,
            source,
            tmp_path / "state",
            tmp_path / "host-pi",
        )


def test_lowercase_missing_object_is_not_a_docker_failure(
    tmp_path: Path, monkeypatch
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")

    def missing(command, *, capture=False, check=True):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="error: no such object: cyclo-missing\n",
        )

    monkeypatch.setattr(docker, "_run", missing)

    assert docker.container_running(selected, system=SYSTEM) is False


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_container_running_rejects_nonoperational_docker_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "verified-container-id",
            "Config": {
                "Labels": {
                    "io.cyclo.system": SYSTEM,
                    "io.cyclo.kind": "team",
                    "io.cyclo.instance": selected.id,
                    "cyclo.launch": selected.launch_id,
                }
            },
            "State": {"Running": True, flag: True},
        },
    )

    assert docker.container_running(selected, system=SYSTEM) is False


def test_team_commands_use_the_verified_immutable_container_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    source = tmp_path / "task.md"
    source.write_text("Create a UART.\n", encoding="utf-8")
    source.chmod(0o644)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: owned_container_info(selected),
    )

    def record(command, **_kwargs):
        recorded = list(command)
        if recorded[:3] == ["docker", "cp", "--archive"]:
            staged = Path(recorded[3])
            assert staged != source
            assert staged.read_text(encoding="utf-8") == "Create a UART.\n"
            assert staged.stat().st_uid == os.getuid()
            assert staged.stat().st_gid == os.getgid()
            assert staged.stat().st_mode & 0o777 == 0o600
            recorded[3] = "<private-task-spec>"
        commands.append(recorded)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker, "_run", record)

    docker.logs(selected, system=SYSTEM, follow=False)
    docker.copy_to(
        selected,
        source,
        "/tmp/task.md",
        system=SYSTEM,
    )
    docker.exec(
        selected,
        ["/agentws/bin/task-list"],
        system=SYSTEM,
        check=False,
    )

    assert commands == [
        ["docker", "logs", "verified-container-id"],
        [
            "docker",
            "cp",
            "--archive",
            "<private-task-spec>",
            "verified-container-id:/tmp/task.md",
        ],
        [
            "docker",
            "exec",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "verified-container-id",
            "/agentws/bin/task-list",
        ],
    ]


def test_current_published_port_uses_the_verified_immutable_container_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: owned_container_info(selected),
    )

    def record(command, **_kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="127.0.0.1:4317\n",
            stderr="",
        )

    monkeypatch.setattr(docker, "_run", record)

    assert docker.current_published_port(selected, system=SYSTEM) == 4317
    assert commands == [
        ["docker", "port", "verified-container-id", "4137/tcp"],
    ]


def test_every_team_operation_rejects_a_foreign_same_name_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    commands: list[list[str]] = []
    foreign = owned_container_info(selected, system="ba9876543210")
    monkeypatch.setattr(docker, "_inspect_container", lambda _name: foreign)
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    operations = (
        lambda: docker.container_running(selected, system=SYSTEM),
        lambda: docker.container_lifecycle_active(selected, system=SYSTEM),
        lambda: docker.current_published_port(selected, system=SYSTEM),
        lambda: docker.logs(selected, system=SYSTEM, follow=False),
        lambda: docker.copy_to(
            selected, tmp_path / "task.md", "/tmp/task.md", system=SYSTEM
        ),
        lambda: docker.exec(
            selected, ["/agentws/bin/task-list"], system=SYSTEM
        ),
        lambda: docker.wait_ready(
            selected, None, system=SYSTEM, timeout=0.01
        ),
    )
    for operation in operations:
        with pytest.raises(CycloError, match="non-Cyclo container"):
            operation()

    assert commands == []


def test_current_team_operations_reject_a_replaced_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    selected.launch_id = "1" * 32
    replacement = owned_container_info(
        selected, launch_id="2" * 32, running=False
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(docker, "_inspect_container", lambda _name: replacement)
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert (
        docker.previous_launch_lifecycle_state(selected, system=SYSTEM)
        is DockerContainerState.STOPPED
    )
    operations = (
        lambda: docker.container_running(selected, system=SYSTEM),
        lambda: docker.current_published_port(selected, system=SYSTEM),
        lambda: docker.logs(selected, system=SYSTEM, follow=False),
        lambda: docker.copy_to(
            selected, tmp_path / "task.md", "/tmp/task.md", system=SYSTEM
        ),
        lambda: docker.exec(
            selected, ["/agentws/bin/task-list"], system=SYSTEM
        ),
        lambda: docker.wait_ready(
            selected, None, system=SYSTEM, timeout=0.01
        ),
    )
    for operation in operations:
        with pytest.raises(CycloError, match="launch identity changed"):
            operation()

    assert commands == []


def test_start_rejects_a_container_with_the_wrong_launch_identity(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = Docker()
    selected = instance(team_repo, tmp_path / "project")
    selected.launch_id = "1" * 32
    spec = ContainerSpec(
        instance=selected,
        team=load_team(team_repo),
        project=tmp_path / "project",
        runtime_root=tmp_path / "runtime",
        tasks_dir=tmp_path / "tasks",
        jobs_dir=tmp_path / "jobs",
        agents_dir=tmp_path / "agents",
        pi_root=tmp_path / "pi",
        provider_socket_dir=Path(selected.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )
    inspections = iter(
        (
            None,
            owned_container_info(selected, launch_id="2" * 32),
        )
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker, "_inspect_container", lambda _name: next(inspections)
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="launch identity changed"):
        docker.start(spec)

    assert commands and commands[0][:2] == ["docker", "run"]


def test_start_force_removes_a_dead_previous_launch(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = Docker()
    project = tmp_path / "project"
    project.mkdir()
    selected = instance(team_repo, project)
    spec = ContainerSpec(
        instance=selected,
        team=load_team(team_repo),
        project=project,
        runtime_root=tmp_path / "runtime",
        tasks_dir=tmp_path / "tasks",
        jobs_dir=tmp_path / "jobs",
        agents_dir=tmp_path / "agents",
        pi_root=tmp_path / "pi",
        provider_socket_dir=Path(selected.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )
    previous = owned_container_info(selected, running=False)
    previous["State"] = {
        "Running": False,
        "Dead": True,
        "Status": "dead",
    }
    inspections = iter((previous, owned_container_info(selected)))
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: next(inspections),
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert docker.start(spec) is None
    assert commands[0] == [
        "docker",
        "rm",
        "--force",
        "verified-container-id",
    ]
    assert commands[1][:3] == ["docker", "run", "--detach"]


def test_start_never_replaces_a_different_stopped_launch(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = Docker()
    project = tmp_path / "project"
    project.mkdir()
    selected = instance(team_repo, project)
    selected.launch_id = "1" * 32
    spec = ContainerSpec(
        instance=selected,
        team=load_team(team_repo),
        project=project,
        runtime_root=tmp_path / "runtime",
        tasks_dir=tmp_path / "tasks",
        jobs_dir=tmp_path / "jobs",
        agents_dir=tmp_path / "agents",
        pi_root=tmp_path / "pi",
        provider_socket_dir=Path(selected.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: owned_container_info(
            selected,
            launch_id="2" * 32,
            running=False,
        ),
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="launch identity changed"):
        docker.start(spec)

    assert commands == []


def test_inactive_launch_removal_is_exact_and_never_stops_an_active_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    selected.launch_id = "1" * 32
    info = owned_container_info(selected)
    commands: list[list[str]] = []
    monkeypatch.setattr(docker, "_inspect_container", lambda _name: info)
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="refusing to replace active"):
        docker.remove_inactive_launch(
            selected.container_name,
            selected.id,
            expected_system=SYSTEM,
            expected_launch=selected.launch_id,
        )
    assert commands == []

    info["State"] = {"Running": False, "Dead": True, "Status": "dead"}
    assert docker.remove_inactive_launch(
        selected.container_name,
        selected.id,
        expected_system=SYSTEM,
        expected_launch=selected.launch_id,
    )
    assert commands == [
        ["docker", "rm", "--force", "verified-container-id"],
    ]


def test_start_validates_the_complete_command_before_replacing_a_container(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = Docker()
    project = tmp_path / "project"
    project.mkdir()
    selected = instance(team_repo, project)
    spec = ContainerSpec(
        instance=selected,
        team=load_team(team_repo),
        project=project,
        runtime_root=tmp_path / "runtime",
        tasks_dir=tmp_path / "tasks",
        jobs_dir=tmp_path / "jobs",
        agents_dir=tmp_path / "agents",
        pi_root=tmp_path / "pi",
        provider_socket_dir=Path(selected.provider_socket_path).parent,
        system=SYSTEM,
        port=0,
    )
    selected.provider_socket_path = str(
        tmp_path / "different-provider" / "component.sock"
    )
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: pytest.fail(
            "invalid launch must not inspect or replace the old container"
        ),
    )

    with pytest.raises(CycloError, match="provider socket does not match"):
        docker.start(spec)


def test_container_removal_uses_verified_immutable_id(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "verified-container-id",
            "Config": {
                "Labels": {
                    "io.cyclo.system": SYSTEM,
                    "io.cyclo.kind": "team",
                    "io.cyclo.instance": "alpha",
                    "cyclo.launch": "0" * 32,
                }
            },
            "State": {"Running": True},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    docker.stop_remove(
        f"cyclo-{SYSTEM}-team-alpha",
        "alpha",
        expected_system=SYSTEM,
        expected_launch="0" * 32,
    )

    assert commands == [
        ["docker", "stop", "--timeout", "30", "verified-container-id"],
        ["docker", "rm", "verified-container-id"],
    ]


def test_dead_container_removal_is_forced(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "verified-container-id",
            "Config": {
                "Labels": {
                    "io.cyclo.system": SYSTEM,
                    "io.cyclo.kind": "team",
                    "io.cyclo.instance": "alpha",
                    "cyclo.launch": "0" * 32,
                }
            },
            "State": {"Running": False, "Dead": True, "Status": "dead"},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    docker.stop_remove(
        f"cyclo-{SYSTEM}-team-alpha",
        "alpha",
        expected_system=SYSTEM,
        expected_launch="0" * 32,
    )

    assert commands == [
        ["docker", "rm", "--force", "verified-container-id"],
    ]


def test_container_removal_rejects_reused_instance_launch(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "replacement-container-id",
            "Config": {
                "Labels": {
                    "io.cyclo.system": SYSTEM,
                    "io.cyclo.kind": "team",
                    "io.cyclo.instance": "alpha",
                    "cyclo.launch": "1" * 32,
                }
            },
            "State": {"Running": True},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="launch identity changed"):
        docker.stop_remove(
            f"cyclo-{SYSTEM}-team-alpha",
            "alpha",
            expected_system=SYSTEM,
            expected_launch="0" * 32,
        )

    assert commands == []


def test_container_removal_rejects_foreign_label(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "foreign-container-id",
            "Config": {"Labels": {"cyclo.instance": "someone-else"}},
            "State": {"Running": False},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="non-Cyclo container"):
        docker.stop_remove(
            f"cyclo-{SYSTEM}-team-alpha",
            "alpha",
            expected_system=SYSTEM,
            expected_launch="0" * 32,
        )

    assert commands == []


def test_container_removal_rejects_another_installation(monkeypatch) -> None:
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "foreign-container-id",
            "Config": {
                "Labels": {
                    "io.cyclo.system": "ba9876543210",
                    "io.cyclo.kind": "team",
                    "io.cyclo.instance": "alpha",
                }
            },
            "State": {"Running": False},
        },
    )

    with pytest.raises(CycloError, match="non-Cyclo container"):
        docker.stop_remove(
            f"cyclo-{SYSTEM}-team-alpha",
            "alpha",
            expected_system=SYSTEM,
            expected_launch="0" * 32,
        )


def test_container_removal_rejects_invalid_launch_before_inspection(
    monkeypatch,
) -> None:
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: pytest.fail("invalid identity must fail before inspection"),
    )

    with pytest.raises(CycloError, match="invalid launch identity"):
        docker.stop_remove(
            f"cyclo-{SYSTEM}-team-alpha",
            "alpha",
            expected_system=SYSTEM,
            expected_launch="",
        )


def test_network_removal_refuses_attached_containers(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_network",
        lambda _name: {
            "Id": "verified-network-id",
            "Labels": {
                "io.cyclo.system": SYSTEM,
                "io.cyclo.kind": "team-network",
                "io.cyclo.instance": "alpha",
            },
            "Containers": {
                "verified-gateway-id": {"Name": "cyclo-gateway-test"}
            },
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="containers remain attached"):
        docker.remove_network(
            f"cyclo-{SYSTEM}-team-alpha-net", "alpha", system=SYSTEM
        )

    assert commands == []


def test_network_creation_uses_installation_identity(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha-net"
    commands: list[list[str]] = []
    inspections = iter(
        (
            None,
            {
                "Id": "verified-network-id",
                "Labels": {
                    "io.cyclo.system": SYSTEM,
                    "io.cyclo.kind": "team-network",
                    "io.cyclo.instance": "alpha",
                },
                "Internal": True,
                "Containers": {},
            },
        )
    )
    monkeypatch.setattr(docker, "_inspect_network", lambda _name: next(inspections))
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert docker.ensure_network(name, "alpha", system=SYSTEM, offline=True) == (
        "verified-network-id"
    )
    assert commands == [
        [
            "docker",
            "network",
            "create",
            "--label",
            f"io.cyclo.system={SYSTEM}",
            "--label",
            "io.cyclo.kind=team-network",
            "--label",
            "io.cyclo.instance=alpha",
            "--internal",
            name,
        ]
    ]


def test_network_removal_uses_verified_resource_id(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_network",
        lambda _name: {
            "Id": "verified-network-id",
            "Labels": {
                "io.cyclo.system": SYSTEM,
                "io.cyclo.kind": "team-network",
                "io.cyclo.instance": "alpha",
            },
            "Containers": {},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    docker.remove_network(
        f"cyclo-{SYSTEM}-team-alpha-net", "alpha", system=SYSTEM
    )

    assert commands == [
        ["docker", "network", "rm", "verified-network-id"]
    ]
