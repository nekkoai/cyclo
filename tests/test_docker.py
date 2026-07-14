from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cyclo.docker import Docker, ContainerSpec, container_command, validate_mount_boundaries
from cyclo.errors import CycloError
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
        project_read_only=True,
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
    spec = ContainerSpec(
        instance=instance(team_repo, project_repo),
        team=load_team(team_repo),
        project=project_repo,
        runtime_root=run,
        tasks_dir=queue / "tasks",
        jobs_dir=queue / "jobs",
        agents_dir=queue / "agents",
        pi_root=pi,
        gateway_container="cyclo-gateway-test",
        port=0,
    )

    command = container_command(spec)
    rendered = " ".join(command)

    assert command[:3] == ["docker", "run", "--detach"]
    assert "--publish" not in command
    assert "AGENTWS_TEAM_ROSTER=/team/team" in command
    assert "AGENTWS_WORKSPACE=/workspace" in command
    assert f"type=bind,src={team_repo},dst=/team,readonly" in command
    assert f"type=bind,src={project_repo},dst=/workspace,readonly" in command
    assert f"type=bind,src={run},dst=/agentws,readonly" in command
    assert f"type=bind,src={pi},dst=/home/cyclo/.pi,readonly" in command
    assert f"type=bind,src={queue / 'tasks'},dst=/agentws/tasks" in command
    assert "gateway-token" not in rendered
    assert "/var/run/docker.sock" not in rendered
    assert str(Path.home() / ".pi" / "agent") not in rendered
    assert "--security-opt" in command
    for name, value in retry_values.items():
        assert f"{name}={value}" in command
    assert "AGENTWS_UNSAFE_UNDOCUMENTED" not in rendered


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


def test_container_removal_uses_verified_immutable_id(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "verified-container-id",
            "Config": {"Labels": {"cyclo.instance": "alpha"}},
            "State": {"Running": True},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    docker.stop_remove("cyclo-alpha", "alpha")

    assert commands == [
        ["docker", "stop", "--timeout", "30", "verified-container-id"],
        ["docker", "rm", "verified-container-id"],
    ]


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


def test_gateway_connection_uses_verified_resource_ids(monkeypatch) -> None:
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

    docker.connect_gateway(
        "verified-network-id", "verified-gateway-id", "cyclo-gateway-test"
    )

    assert commands == [
        [
            "docker",
            "network",
            "connect",
            "--alias",
            "cyclo-gateway-test",
            "verified-network-id",
            "verified-gateway-id",
        ]
    ]
