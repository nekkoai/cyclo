from __future__ import annotations

import json
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
    selected_docker_endpoint,
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
CONTAINER_ID = "a" * 64
PREVIOUS_CONTAINER_ID = "b" * 64
REPLACEMENT_CONTAINER_ID = "c" * 64
NETWORK_ID = "d" * 64
ATTACHED_CONTAINER_ID = "e" * 64


@pytest.fixture
def standard_docker_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "cyclo.docker.selected_docker_endpoint",
        lambda environment=None: "unix:///var/run/docker.sock",
    )


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
    container_id: str = CONTAINER_ID,
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
        "Name": f"/{selected.container_name}",
        "Config": {"Labels": labels},
        "State": {"Running": running},
    }


def team_container_info(
    name: str,
    instance_id: str,
    *,
    system: str = SYSTEM,
    container_id: str = CONTAINER_ID,
    launch_id: str = "0" * 32,
    running: bool = True,
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Config": {
            "Labels": {
                "io.cyclo.system": system,
                "io.cyclo.kind": "team",
                "io.cyclo.instance": instance_id,
                "cyclo.launch": launch_id,
            }
        },
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

    assert command[:3] == ["docker", "create", "--name"]
    assert command[3] == selected_instance.container_name
    assert "--publish" not in command
    assert command[command.index("--network") + 1] == "none"
    assert selected_instance.network_name not in command
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

    selected_instance.offline = False
    online_command = container_command(spec)
    assert (
        online_command[online_command.index("--network") + 1]
        == selected_instance.network_name
    )
    assert "--publish" in online_command


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


def test_mounts_must_not_cover_host_credentials(
    tmp_path: Path,
    standard_docker_endpoint,
) -> None:
    team = tmp_path / "team"
    project = tmp_path / "home"
    credentials = project / ".pi" / "agent"
    team.mkdir()
    credentials.mkdir(parents=True)

    with pytest.raises(CycloError, match="credential"):
        validate_mount_boundaries(team, project, tmp_path / "state", credentials)


def test_selected_context_and_rootless_docker_sockets_are_protected(
    tmp_path: Path, monkeypatch
) -> None:
    team = tmp_path / "team"
    project = tmp_path / "project"
    team.mkdir()
    project.mkdir()
    socket = project / "runtime" / "docker.sock"
    ignored = tmp_path / "ignored" / "docker.sock"
    monkeypatch.setenv("DOCKER_CONTEXT", "custom")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{ignored}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))

    def inspect(command, **options):
        assert options["env"] is None
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(f"unix://{socket}") + "\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", inspect)

    assert socket.resolve() in docker_socket_paths()
    assert ignored.resolve() not in docker_socket_paths()
    assert (tmp_path / "xdg-runtime" / "docker.sock").resolve() in docker_socket_paths()
    with pytest.raises(CycloError, match="Docker socket"):
        validate_mount_boundaries(
            team,
            project,
            tmp_path / "state",
            tmp_path / "host-pi",
        )


def test_selected_docker_endpoint_delegates_precedence_to_context_inspect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context_socket = tmp_path / "context" / "docker.sock"
    ignored_host = tmp_path / "host" / "docker.sock"
    environment = {
        "DOCKER_CONTEXT": "selected-context",
        "DOCKER_HOST": f"unix://{ignored_host}",
        "PATH": os.environ.get("PATH", ""),
    }
    calls: list[tuple[list[str], dict[str, object]]] = []

    def inspect(command, **options):
        calls.append((list(command), dict(options)))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(f"unix://{context_socket}") + "\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", inspect)

    assert selected_docker_endpoint() == f"unix://{context_socket}"
    assert selected_docker_endpoint(environment) == f"unix://{context_socket}"
    assert calls[0][1]["env"] is None
    assert calls[1][1]["env"] == environment
    assert calls[1][0] == [
        "docker",
        "context",
        "inspect",
        "--format",
        '{{json (index .Endpoints "docker").Host}}',
    ]
    assert calls[1][1]["timeout"] == 5.0
    assert calls[1][1]["check"] is False


@pytest.mark.parametrize(
    "response",
    (
        "",
        "\n",
        "not-json\n",
        "null\n",
        "123\n",
        '""\n',
        '"unix:relative/docker.sock"\n',
        '"unix://relative/docker.sock"\n',
        '"unix:///tmp/docker.sock\\nother"\n',
        '"unix:///tmp/one.sock"\n"unix:///tmp/two.sock"\n',
    ),
)
def test_selected_docker_endpoint_rejects_malformed_responses(
    response: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(
            command,
            0,
            stdout=response,
            stderr="",
        ),
    )

    with pytest.raises(CycloError, match="selected Docker endpoint"):
        selected_docker_endpoint()


@pytest.mark.parametrize("failure", ("nonzero", "timeout", "missing"))
def test_selected_docker_endpoint_fails_closed(
    failure: str,
    monkeypatch,
) -> None:
    def inspect(command, **options):
        if failure == "nonzero":
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="selected context is unavailable\n",
            )
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", inspect)

    with pytest.raises(CycloError, match="selected Docker endpoint"):
        selected_docker_endpoint()


@pytest.mark.parametrize(
    ("endpoint", "selected_socket"),
    (
        (
            "unix:///tmp/cyclo-custom-context/docker.sock",
            Path("/tmp/cyclo-custom-context/docker.sock"),
        ),
        (
            f"unix:///run/user/{os.getuid()}/custom/docker.sock",
            Path(f"/run/user/{os.getuid()}/custom/docker.sock"),
        ),
        ("tcp://127.0.0.1:2375", None),
    ),
)
def test_docker_socket_paths_include_only_selected_unix_endpoints(
    endpoint: str,
    selected_socket: Path | None,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(endpoint) + "\n",
            stderr="",
        ),
    )

    paths = docker_socket_paths({})

    if selected_socket is None:
        assert set(paths) == {
            Path("/var/run/docker.sock").resolve(),
            Path(f"/run/user/{os.getuid()}/docker.sock").resolve(),
            (Path.home() / ".docker" / "run" / "docker.sock").resolve(),
        }
    else:
        assert selected_socket.resolve() in paths


def test_docker_socket_paths_reject_an_unresolvable_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    loop = tmp_path / "docker.sock"
    loop.symlink_to(loop.name)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(f"unix://{loop}") + "\n",
            stderr="",
        ),
    )

    with pytest.raises(CycloError, match="selected Docker endpoint"):
        docker_socket_paths({})


@pytest.mark.parametrize("source", [Path("/proc/self"), Path("/sys"), Path("/dev"), Path("/run")])
def test_host_pseudo_filesystems_cannot_be_mounted(
    source: Path,
    tmp_path: Path,
    standard_docker_endpoint,
) -> None:
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

    def missing(arguments, **_options):
        assert list(arguments) == [
            "container",
            "inspect",
            "--",
            selected.container_name,
        ]
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout="",
            stderr=f"error: no such object: {selected.container_name}\n",
        )

    monkeypatch.setattr(docker, "call", missing)

    assert docker.container_running(selected, system=SYSTEM) is False


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead"])
def test_container_running_rejects_nonoperational_docker_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: {
            **owned_container_info(selected),
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
        "inspect",
        lambda kind, reference, **_options: owned_container_info(selected),
    )

    def record(arguments, **_options):
        recorded = list(arguments)
        if recorded[:2] == ["cp", "--archive"]:
            staged = Path(recorded[2])
            assert staged != source
            assert staged.read_text(encoding="utf-8") == "Create a UART.\n"
            assert staged.stat().st_uid == os.getuid()
            assert staged.stat().st_gid == os.getgid()
            assert staged.stat().st_mode & 0o777 == 0o600
            recorded[2] = "<private-task-spec>"
        commands.append(recorded)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(docker, "call", record)

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
        ["logs", CONTAINER_ID],
        [
            "cp",
            "--archive",
            "<private-task-spec>",
            f"{CONTAINER_ID}:/tmp/task.md",
        ],
        [
            "exec",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            CONTAINER_ID,
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
        "inspect",
        lambda kind, reference, **_options: owned_container_info(selected),
    )

    def record(arguments, **_options):
        commands.append(list(arguments))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout="127.0.0.1:4317\n",
            stderr="",
        )

    monkeypatch.setattr(docker, "call", record)

    assert docker.current_published_port(selected, system=SYSTEM) == 4317
    assert commands == [
        ["port", CONTAINER_ID, "4137/tcp"],
    ]


def test_every_team_operation_rejects_a_foreign_same_name_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker()
    selected = instance(tmp_path / "team", tmp_path / "project")
    commands: list[list[str]] = []
    foreign = owned_container_info(selected, system="ba9876543210")
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: foreign,
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
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
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: replacement,
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
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


def test_start_creates_inspects_and_starts_verified_immutable_id(
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
    inspections = iter((None, owned_container_info(selected, running=False)))
    events: list[tuple[str, object]] = []

    def inspect(kind, reference, **_options):
        events.append(
            ("inspect", (kind, reference, _options["missing"]))
        )
        return next(inspections)

    def call(arguments, **_options):
        command = list(arguments)
        events.append(("call", command))
        stdout = f"{CONTAINER_ID}\n" if command[0] == "create" else ""
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    monkeypatch.setattr(docker, "inspect", inspect)
    monkeypatch.setattr(docker, "call", call)

    assert docker.start(spec) is None
    assert events == [
        ("inspect", ("container", selected.container_name, True)),
        ("call", container_command(spec)[1:]),
        ("inspect", ("container", CONTAINER_ID, False)),
        ("call", ["start", CONTAINER_ID]),
    ]


def test_start_rejects_a_created_container_with_the_wrong_launch_identity(
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
    wrong_launch = owned_container_info(
        selected,
        launch_id="2" * 32,
        running=False,
    )
    inspections = iter((None, wrong_launch))
    inspection_calls: list[tuple[str, str, bool]] = []
    commands: list[list[str]] = []

    def inspect(kind, reference, **options):
        inspection_calls.append((kind, reference, options["missing"]))
        return next(inspections)

    def call(arguments, **_options):
        command = list(arguments)
        commands.append(command)
        stdout = f"{CONTAINER_ID}\n" if command[0] == "create" else ""
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    monkeypatch.setattr(docker, "inspect", inspect)
    monkeypatch.setattr(docker, "call", call)

    with pytest.raises(CycloError, match="launch identity changed"):
        docker.start(spec)

    assert commands == [container_command(spec)[1:]]
    assert inspection_calls == [
        ("container", selected.container_name, True),
        ("container", CONTAINER_ID, False),
    ]


def test_start_failure_reinspects_and_force_removes_exact_created_id(
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
    created = owned_container_info(selected, running=False)
    inspections = iter((None, created, created, created))
    events: list[tuple[str, object]] = []

    def inspect(kind, reference, **_options):
        events.append(
            ("inspect", (kind, reference, _options["missing"]))
        )
        return next(inspections)

    def call(arguments, **_options):
        command = list(arguments)
        events.append(("call", command))
        if command[0] == "create":
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"{CONTAINER_ID}\n",
                "",
            )
        if command[0] == "start":
            raise CycloError("simulated start failure")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(docker, "inspect", inspect)
    monkeypatch.setattr(docker, "call", call)

    with pytest.raises(CycloError, match="simulated start failure"):
        docker.start(spec)

    assert events == [
        ("inspect", ("container", selected.container_name, True)),
        ("call", container_command(spec)[1:]),
        ("inspect", ("container", CONTAINER_ID, False)),
        ("call", ["start", CONTAINER_ID]),
        ("inspect", ("container", CONTAINER_ID, True)),
        ("inspect", ("container", CONTAINER_ID, True)),
        ("call", ["rm", "--force", CONTAINER_ID]),
    ]


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
    previous["Id"] = PREVIOUS_CONTAINER_ID
    previous["State"] = {
        "Running": False,
        "Dead": True,
        "Status": "dead",
    }
    created = owned_container_info(selected, running=False)
    commands: list[list[str]] = []
    create_seen = False

    def inspect(kind, reference, **_options):
        if reference == PREVIOUS_CONTAINER_ID:
            return previous
        if reference == CONTAINER_ID:
            return created
        if reference == selected.container_name:
            return created if create_seen else previous
        pytest.fail(f"unexpected Docker inspection: {kind} {reference}")

    def call(arguments, **_options):
        nonlocal create_seen
        command = list(arguments)
        commands.append(command)
        if command[0] == "create":
            create_seen = True
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"{CONTAINER_ID}\n",
                "",
            )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(docker, "inspect", inspect)
    monkeypatch.setattr(docker, "call", call)

    assert docker.start(spec) is None
    assert commands == [
        ["rm", "--force", PREVIOUS_CONTAINER_ID],
        container_command(spec)[1:],
        ["start", CONTAINER_ID],
    ]


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
        "inspect",
        lambda kind, reference, **_options: owned_container_info(
            selected,
            container_id=REPLACEMENT_CONTAINER_ID,
            launch_id="2" * 32,
            running=False,
        ),
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
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
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: info,
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="refusing to remove active"):
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
        ["rm", "--force", CONTAINER_ID],
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
        "inspect",
        lambda kind, reference, **_options: pytest.fail(
            "invalid launch must not inspect or replace the old container"
        ),
    )

    with pytest.raises(CycloError, match="provider socket does not match"):
        docker.start(spec)


def test_container_removal_uses_verified_immutable_id(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: team_container_info(name, "alpha"),
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    docker.stop_remove(
        name,
        "alpha",
        expected_system=SYSTEM,
        expected_launch="0" * 32,
    )

    assert commands == [
        ["stop", "--timeout", "30", CONTAINER_ID],
        ["rm", CONTAINER_ID],
    ]


def test_dead_container_removal_is_forced(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha"
    commands: list[list[str]] = []
    info = team_container_info(name, "alpha", running=False)
    info["State"] = {"Running": False, "Dead": True, "Status": "dead"}
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: info,
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    docker.stop_remove(
        name,
        "alpha",
        expected_system=SYSTEM,
        expected_launch="0" * 32,
    )

    assert commands == [
        ["rm", "--force", CONTAINER_ID],
    ]


def test_container_removal_rejects_reused_instance_launch(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: team_container_info(
            name,
            "alpha",
            container_id=REPLACEMENT_CONTAINER_ID,
            launch_id="1" * 32,
        ),
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="launch identity changed"):
        docker.stop_remove(
            name,
            "alpha",
            expected_system=SYSTEM,
            expected_launch="0" * 32,
        )

    assert commands == []


def test_container_removal_rejects_foreign_label(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: {
            "Id": REPLACEMENT_CONTAINER_ID,
            "Name": f"/{name}",
            "Config": {"Labels": {"cyclo.instance": "someone-else"}},
            "State": {"Running": False},
        },
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="non-Cyclo container"):
        docker.stop_remove(
            name,
            "alpha",
            expected_system=SYSTEM,
            expected_launch="0" * 32,
        )

    assert commands == []


def test_container_removal_rejects_another_installation(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha"
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: team_container_info(
            name,
            "alpha",
            system="ba9876543210",
            container_id=REPLACEMENT_CONTAINER_ID,
            running=False,
        ),
    )

    with pytest.raises(CycloError, match="non-Cyclo container"):
        docker.stop_remove(
            name,
            "alpha",
            expected_system=SYSTEM,
            expected_launch="0" * 32,
        )


def test_container_removal_rejects_invalid_launch_before_mutation(
    monkeypatch,
) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha"
    inspections: list[tuple[str, str]] = []
    commands: list[list[str]] = []

    def inspect(kind, reference, **_options):
        inspections.append((kind, reference))
        return team_container_info(name, "alpha", running=False)

    monkeypatch.setattr(
        docker,
        "inspect",
        inspect,
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="invalid launch identity"):
        docker.stop_remove(
            name,
            "alpha",
            expected_system=SYSTEM,
            expected_launch="",
        )

    assert inspections == [("container", name)]
    assert commands == []


def test_network_removal_refuses_attached_containers(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: {
            "Id": NETWORK_ID,
            "Labels": {
                "io.cyclo.system": SYSTEM,
                "io.cyclo.kind": "team-network",
                "io.cyclo.instance": "alpha",
            },
            "Containers": {
                ATTACHED_CONTAINER_ID: {"Name": "cyclo-gateway-test"}
            },
        },
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    with pytest.raises(CycloError, match="containers remain attached"):
        docker.remove_network(
            f"cyclo-{SYSTEM}-team-alpha-net", "alpha", system=SYSTEM
        )

    assert commands == []


def test_online_network_creation_uses_installation_identity(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha-net"
    commands: list[list[str]] = []
    inspections = iter(
        (
            None,
            {
                "Id": NETWORK_ID,
                "Labels": {
                    "io.cyclo.system": SYSTEM,
                    "io.cyclo.kind": "team-network",
                    "io.cyclo.instance": "alpha",
                },
                "Internal": False,
                "Containers": {},
            },
        )
    )
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: next(inspections),
    )
    monkeypatch.setattr(
        docker,
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    assert docker.ensure_network(name, "alpha", system=SYSTEM) == NETWORK_ID
    assert commands == [
        [
            "network",
            "create",
            "--label",
            f"io.cyclo.system={SYSTEM}",
            "--label",
            "io.cyclo.kind=team-network",
            "--label",
            "io.cyclo.instance=alpha",
            name,
        ]
    ]


def test_online_network_rejects_internal_bridge(monkeypatch) -> None:
    docker = Docker()
    name = f"cyclo-{SYSTEM}-team-alpha-net"
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: {
            "Id": NETWORK_ID,
            "Labels": {
                "io.cyclo.system": SYSTEM,
                "io.cyclo.kind": "team-network",
                "io.cyclo.instance": "alpha",
            },
            "Internal": True,
            "Containers": {},
        },
    )

    with pytest.raises(CycloError, match="internal mode"):
        docker.ensure_network(name, "alpha", system=SYSTEM)


def test_network_removal_uses_verified_resource_id(monkeypatch) -> None:
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda kind, reference, **_options: {
            "Id": NETWORK_ID,
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
        "call",
        lambda arguments, **_options: commands.append(list(arguments))
        or subprocess.CompletedProcess(arguments, 0, stdout="", stderr=""),
    )

    docker.remove_network(
        f"cyclo-{SYSTEM}-team-alpha-net", "alpha", system=SYSTEM
    )

    assert commands == [
        ["network", "rm", NETWORK_ID]
    ]
