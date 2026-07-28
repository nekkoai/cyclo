from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.cli import cmd_repair
from cyclo.docker import Docker, DockerContainerState
from cyclo.errors import CycloError
from cyclo.instance_lifecycle import intended_running_instances, stop_instance
from cyclo.installation import team_container_name, team_network_name
from cyclo.project import ProjectMount
from cyclo.project_run import (
    RunBinding,
    preflight_binding,
    validate_running_mount_boundaries,
)
from cyclo.state import Instance, StateStore
from cyclo.team import load_team

CONTAINER_ID = "a" * 64


def _instance(
    identifier: str,
    team: Path,
    project: Path,
    *,
    intent: str = "running",
) -> Instance:
    return Instance(
        id=identifier,
        team_name=identifier,
        team_path=str(team),
        project_path=str(project),
        generation="test-generation",
        providers=[],
        models=[],
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=False,
        launch_id="0" * 32,
        intent=intent,
    )


def _lifecycle_info(
    flag: str, selected: Instance, *, system: str
) -> dict[str, object]:
    return {
        "Id": CONTAINER_ID,
        "Name": f"/{selected.container_name}",
        "Config": {
            "Labels": {
                "io.cyclo.system": system,
                "io.cyclo.kind": "team",
                "io.cyclo.instance": selected.id,
                "cyclo.launch": selected.launch_id,
            }
        },
        "State": {"Running": True, flag: True},
    }


def _persist(store: StateStore, selected: Instance) -> None:
    selected.container_name = team_container_name(store.system, selected.id)
    selected.network_name = team_network_name(store.system, selected.id)
    store.save(selected)


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("Paused", DockerContainerState.PAUSED),
        ("Restarting", DockerContainerState.RESTARTING),
    ],
)
def test_paused_and_restarting_are_lifecycle_active_but_not_operational(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    expected: DockerContainerState,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: _lifecycle_info(
            flag,
            selected,
            system=store.system,
        ),
    )

    assert docker.container_lifecycle_state(selected, system=store.system) is expected
    assert docker.container_lifecycle_active(selected, system=store.system) is True
    assert docker.container_running(selected, system=store.system) is False


def test_intended_running_instances_selects_only_running_intent(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    running = _instance("running", tmp_path / "team", tmp_path / "project")
    running.port = 4137
    stopped = _instance(
        "stopped",
        tmp_path / "team",
        tmp_path / "project",
        intent="stopped",
    )
    deleting = _instance(
        "deleting",
        tmp_path / "team",
        tmp_path / "project",
        intent="deleting",
    )
    for selected in (running, stopped, deleting):
        _persist(store, selected)

    retained = intended_running_instances(store)

    assert [instance.id for instance in retained] == [running.id]
    assert store.load(running.id).intent == "running"
    assert store.load(running.id).port == 4137
    assert store.load(stopped.id).intent == "stopped"
    assert store.load(deleting.id).intent == "deleting"


def test_repair_recovers_an_interrupted_online_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: _lifecycle_info(
            "Running",
            selected,
            system=store.system,
        ),
    )
    readiness: list[tuple[int | None, str]] = []
    monkeypatch.setattr(
        docker,
        "wait_ready",
        lambda _instance, port, *, system, host: readiness.append(
            (port, f"{system}:{host}")
        ),
    )

    def run(command, **_kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0.0.0.0:4317\n",
            stderr="",
        )

    monkeypatch.setattr(docker, "call", run)
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    assert cmd_repair(SimpleNamespace()) == 0

    persisted = store.load(selected.id)
    assert persisted.intent == "running"
    assert persisted.port == 4317
    assert commands == [
        ["port", CONTAINER_ID, "4137/tcp"],
    ]
    assert readiness == [(4317, f"{store.system}:127.0.0.1")]


def test_repair_does_not_persist_a_port_before_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: _lifecycle_info(
            "Running",
            selected,
            system=store.system,
        ),
    )
    monkeypatch.setattr(
        docker,
        "current_published_port",
        lambda *_args, **_kwargs: 4317,
    )
    monkeypatch.setattr(
        docker,
        "wait_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CycloError("AgentWS is not ready")
        ),
    )
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    with pytest.raises(CycloError, match="readiness"):
        cmd_repair(SimpleNamespace())

    assert store.load(selected.id).port is None


def test_repair_checks_readiness_for_an_existing_published_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.port = 4317
    _persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: _lifecycle_info(
            "Running",
            selected,
            system=store.system,
        ),
    )
    monkeypatch.setattr(
        docker,
        "current_published_port",
        lambda *_args, **_kwargs: pytest.fail(
            "an existing port must not be rediscovered"
        ),
    )
    readiness: list[int | None] = []
    monkeypatch.setattr(
        docker,
        "wait_ready",
        lambda _instance, port, **_kwargs: readiness.append(port),
    )
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    assert cmd_repair(SimpleNamespace()) == 0
    assert readiness == [4317]
    assert store.load(selected.id).port == 4317


def test_repair_preserves_offline_portless_running_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.offline = True
    _persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: _lifecycle_info(
            "Running",
            selected,
            system=store.system,
        ),
    )
    monkeypatch.setattr(
        docker,
        "current_published_port",
        lambda *_args, **_kwargs: pytest.fail(
            "offline instances have no published AgentWS port"
        ),
        raising=False,
    )
    readiness: list[int | None] = []
    monkeypatch.setattr(
        docker,
        "wait_ready",
        lambda _instance, port, **_kwargs: readiness.append(port),
    )
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    assert cmd_repair(SimpleNamespace()) == 0

    persisted = store.load(selected.id)
    assert persisted.intent == "running"
    assert persisted.port is None
    assert readiness == [None]


def test_repair_rejects_a_foreign_same_name_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    foreign = _lifecycle_info("Paused", selected, system="ba9876543210")
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: foreign,
    )
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    with pytest.raises(CycloError, match="non-Cyclo container"):
        cmd_repair(SimpleNamespace())

    assert store.load(selected.id).intent == "running"


def test_stop_is_independent_of_an_unrelated_corrupt_record(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.port = 4137
    _persist(store, selected)
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not-json\n", encoding="utf-8")
    events: list[str] = []

    class ExactDocker:
        @staticmethod
        def stop_remove(*_args, **_kwargs):
            events.append("container")
            return True

        @staticmethod
        def remove_network(*_args, **_kwargs):
            events.append("network")
            return True

    stop_instance(store, ExactDocker(), selected)  # type: ignore[arg-type]

    assert events == ["container", "network"]
    assert store.load(selected.id).intent == "stopped"
    assert broken.read_text(encoding="utf-8") == "{not-json\n"


def test_stop_uses_one_launch_checked_removal_without_preinspection(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.port = 4137
    _persist(store, selected)
    events: list[tuple[object, ...]] = []

    class RemovalOnlyDocker:
        @staticmethod
        def stop_remove(
            container,
            expected_instance,
            *,
            expected_system,
            expected_launch,
        ):
            events.append(
                (
                    "container",
                    container,
                    expected_instance,
                    expected_system,
                    expected_launch,
                )
            )
            return True

        @staticmethod
        def remove_network(name, expected_instance, *, system):
            events.append(("network", name, expected_instance, system))

    stop_instance(
        store,
        RemovalOnlyDocker(),  # type: ignore[arg-type]
        selected,
    )

    persisted = store.load(selected.id)
    assert persisted.intent == "stopped"
    assert persisted.port is None
    assert events == [
        (
            "container",
            selected.container_name,
            selected.id,
            store.system,
            selected.launch_id,
        ),
        ("network", selected.network_name, selected.id, store.system),
    ]


def test_stop_rejects_a_replaced_persisted_launch_before_side_effects(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    expected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, expected)
    replacement = store.load(expected.id)
    replacement.launch_id = "1" * 32
    store.save(replacement)

    class NoDockerWrites:
        def __getattr__(self, name: str):
            raise AssertionError(f"unexpected Docker operation: {name}")

    with pytest.raises(CycloError, match="was replaced"):
        stop_instance(
            store,
            NoDockerWrites(),  # type: ignore[arg-type]
            expected,
        )

    assert store.load(expected.id).intent == "running"


def test_stop_refuses_to_cancel_deleting_intent(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance(
        "team",
        tmp_path / "team",
        tmp_path / "project",
        intent="deleting",
    )
    selected.port = 4137
    _persist(store, selected)

    class NoDockerWrites:
        def __getattr__(self, name: str):
            raise AssertionError(f"unexpected Docker operation: {name}")

    with pytest.raises(CycloError, match="being deleted"):
        stop_instance(
            store,
            NoDockerWrites(),  # type: ignore[arg-type]
            selected,
        )

    persisted = store.load(selected.id)
    assert persisted.intent == "deleting"
    assert persisted.port == 4137


@pytest.mark.parametrize(
    "state",
    [
        DockerContainerState.RUNNING,
        DockerContainerState.PAUSED,
        DockerContainerState.RESTARTING,
    ],
)
def test_repair_removes_lifecycle_active_team_with_stopped_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: DockerContainerState,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance(
        "team",
        tmp_path / "team",
        tmp_path / "project",
        intent="stopped",
    )
    _persist(store, selected)
    events: list[tuple[object, ...]] = []

    class FakeDocker:
        @staticmethod
        def container_lifecycle_state(instance, *, system):
            assert instance.id == selected.id
            assert system == store.system
            return state

        @staticmethod
        def stop_remove(
            container,
            expected_instance,
            *,
            expected_system,
            expected_launch,
        ):
            events.append(
                (
                    "container",
                    container,
                    expected_instance,
                    expected_system,
                    expected_launch,
                )
            )
            return True

        @staticmethod
        def remove_network(name, expected_instance, *, system):
            events.append(("network", name, expected_instance, system))

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert cmd_repair(SimpleNamespace()) == 0

    repaired = store.load(selected.id)
    assert repaired.intent == "stopped"
    assert repaired.port is None
    assert events == [
        (
            "container",
            selected.container_name,
            selected.id,
            store.system,
            selected.launch_id,
        ),
        ("network", selected.network_name, selected.id, store.system),
    ]


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_preflight_rejects_duplicate_lifecycle_active_container(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", team_repo, project_repo)
    selected.container_name = team_container_name(store.system, selected.id)
    selected.network_name = team_network_name(store.system, selected.id)
    binding = RunBinding(
        team=load_team(team_repo),
        project_root=project_repo,
        instance=selected,
        project_config="",
    )
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, _name, **_kwargs: _lifecycle_info(
            flag,
            selected,
            system=store.system,
        ),
    )

    with pytest.raises(CycloError, match=f"already active \\({flag.lower()}\\)"):
        preflight_binding(binding, store, docker)


def test_preflight_refuses_to_reuse_an_ordinary_deleting_record(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    deleting = _instance(
        "team",
        team_repo,
        project_repo,
        intent="deleting",
    )
    _persist(store, deleting)
    replacement = _instance("team", team_repo, project_repo)
    replacement.container_name = deleting.container_name
    replacement.network_name = deleting.network_name
    binding = RunBinding(
        team=load_team(team_repo),
        project_root=project_repo,
        instance=replacement,
        project_config="",
    )

    class AbsentDocker:
        @staticmethod
        def previous_launch_lifecycle_state(instance, *, system):
            assert instance.id == deleting.id
            assert system == store.system
            return DockerContainerState.ABSENT

    with pytest.raises(CycloError, match="being deleted"):
        preflight_binding(
            binding,
            store,
            AbsentDocker(),  # type: ignore[arg-type]
        )

    assert store.load(deleting.id).intent == "deleting"


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_mount_preflight_includes_lifecycle_active_containers(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    active_project = tmp_path / "active-project"
    nested_project = active_project / "nested"
    active_team = tmp_path / "active-team"
    active_team.mkdir()
    nested_project.mkdir(parents=True)
    running = _instance("running", active_team, active_project)
    _persist(store, running)
    binding = RunBinding(
        team=load_team(team_repo),
        project_root=nested_project,
        instance=_instance("new", team_repo, nested_project),
        project_config="",
        project_mounts=(
            ProjectMount("source", nested_project.resolve(), "rw", 1),
        ),
    )
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _kind, name, **_kwargs: _lifecycle_info(
            flag,
            running,
            system=store.system,
        )
        if name == running.container_name
        else None,
    )

    with pytest.raises(CycloError, match="overlaps project of running instance"):
        validate_running_mount_boundaries(binding, store, docker)
