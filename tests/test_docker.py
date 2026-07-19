from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cyclo.docker import (
    Docker,
    ContainerSpec,
    container_command,
    docker_socket_paths,
    validate_mount_collection,
    validate_mount_boundaries,
    mount,
)
from cyclo.errors import CycloError
from cyclo.project import ProjectMount
from cyclo.state import Instance
from cyclo.team import load_team


def instance(team: Path, project: Path) -> Instance:
    return Instance(
        id="review-team-123",
        team_name="review-team",
        team_path=str(team),
        project_path=str(project),
        generation="deadbeef-dirty-a1",
        providers=["anthropic", "openai-codex"],
        models=["anthropic/claude-test", "openai-codex/gpt-test"],
        container_name="cyclo-review-team-123",
        network_name="cyclo-review-team-123-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=True,
    )


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
        port=0,
        provider_runtime_health_url="http://cyclo-runtime:8788/health",
    )

    command = container_command(spec)
    rendered = " ".join(command)

    assert command[:3] == ["docker", "run", "--detach"]
    assert "--publish" not in command
    assert "AGENTWS_TEAM_ROSTER=/team/team" in command
    assert "AGENTWS_WORKSPACE=/workspace" in command
    assert (
        "CYCLO_PROVIDER_RUNTIME_HEALTH_URL=http://cyclo-runtime:8788/health"
        in command
    )
    assert f"type=bind,src={team_repo},dst=/team,readonly" in command
    assert f"type=bind,src={project_repo},dst=/workspace" in command
    assert f"type=bind,src={project_repo},dst=/workspace,readonly" not in command
    assert f"type=bind,src={run},dst=/agentws,readonly" in command
    assert f"type=bind,src={pi},dst=/home/cyclo/.pi" in command
    assert f"type=bind,src={pi},dst=/home/cyclo/.pi,readonly" not in command
    assert f"type=bind,src={queue / 'tasks'},dst=/agentws/tasks" in command
    assert "gateway-token" not in rendered
    assert "/var/run/docker.sock" not in rendered
    assert str(Path.home() / ".pi" / "agent") not in rendered
    assert "--security-opt" in command
    assert command[command.index("--cap-drop") + 1] == "NET_RAW"
    assert "cyclo.launch=launch-identity" in command
    for name, value in retry_values.items():
        assert f"{name}={value}" in command
    assert "AGENTWS_UNSAFE_UNDOCUMENTED" not in rendered


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
    spec = ContainerSpec(
        instance=instance(team_repo, project_repo),
        team=load_team(team_repo),
        project=project_repo,
        runtime_root=runtime,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
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
    spec = ContainerSpec(
        instance=instance(team_repo, tmp_path),
        team=load_team(team_repo),
        project=tmp_path,
        runtime_root=runtime,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
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
    assert "CYCLO_PROJECT_MANIFEST=/agentws/PROJECT.md" in command
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
    spec = ContainerSpec(
        instance=instance(team_repo, tmp_path),
        team=load_team(team_repo),
        project=tmp_path,
        runtime_root=runtime,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
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


def test_lowercase_missing_object_is_not_a_docker_failure(monkeypatch) -> None:
    docker = Docker()

    def missing(command, *, capture=False, check=True):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="error: no such object: cyclo-missing\n",
        )

    monkeypatch.setattr(docker, "_run", missing)

    assert docker.container_running("cyclo-missing") is False
    assert docker.container_exists("cyclo-missing") is False


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_container_running_rejects_nonoperational_docker_states(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {"State": {"Running": True, flag: True}},
    )

    assert docker.container_running("cyclo-alpha") is False


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
                    "cyclo.instance": "alpha",
                    "cyclo.launch": "launch-alpha",
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

    docker.stop_remove("cyclo-alpha", "alpha", expected_launch="launch-alpha")

    assert commands == [
        ["docker", "stop", "--timeout", "30", "verified-container-id"],
        ["docker", "rm", "verified-container-id"],
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
                    "cyclo.instance": "alpha",
                    "cyclo.launch": "replacement-launch",
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
            "cyclo-alpha", "alpha", expected_launch="original-launch"
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
        docker.stop_remove("cyclo-alpha", "alpha")

    assert commands == []


def test_network_removal_uses_inspected_network_and_member_ids(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_network",
        lambda _name: {
            "Id": "verified-network-id",
            "Labels": {"cyclo.instance": "cyclo-alpha-net"},
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

    docker.remove_network("cyclo-alpha-net", "cyclo-gateway-test")

    assert commands == [
        [
            "docker",
            "network",
            "disconnect",
            "verified-network-id",
            "verified-gateway-id",
        ],
        ["docker", "network", "rm", "verified-network-id"],
    ]


def test_runtime_connection_uses_verified_resource_ids(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_network",
        lambda _name: {
            "Id": "verified-network-id",
            "Labels": {"cyclo.instance": "cyclo-alpha-net"},
            "Containers": {},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    docker.connect_runtime(
        "verified-network-id", "verified-runtime-id", "cyclo-provider-runtime-test"
    )

    assert commands == [
        [
            "docker",
            "network",
            "connect",
            "--alias",
            "cyclo-provider-runtime-test",
            "verified-network-id",
            "verified-runtime-id",
        ]
    ]
