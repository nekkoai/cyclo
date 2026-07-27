from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.cli import cmd_repair
from cyclo.docker import Docker, DockerContainerState
from cyclo.errors import CycloError
from cyclo.instance_lifecycle import active_instances, stop_instance
from cyclo.installation import team_container_name, team_network_name
from cyclo.project import ProjectMount
from cyclo.project_run import (
    RunBinding,
    preflight_binding,
    validate_running_mount_boundaries,
)
from cyclo.state import Instance, StateStore
from cyclo.team import load_team


def _instance(
    identifier: str,
    team: Path,
    project: Path,
    *, active: bool = True,
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
        active=active,
    )


def _lifecycle_info(
    flag: str, selected: Instance, *, system: str
) -> dict[str, object]:
    return {
        "Id": f"{selected.id}-container-id",
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
        "_inspect_container",
        lambda _name: _lifecycle_info(flag, selected, system=store.system),
    )

    assert docker.container_lifecycle_state(selected, system=store.system) is expected
    assert docker.container_lifecycle_active(selected, system=store.system) is True
    assert docker.container_running(selected, system=store.system) is False


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_active_instances_preserves_temporary_lifecycle_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.port = 4137
    _persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: _lifecycle_info(flag, selected, system=store.system),
    )
    stale: list[Instance] = []

    retained = active_instances(store, docker, stale=stale)

    assert [instance.id for instance in retained] == [selected.id]
    assert store.load(selected.id).active is True
    assert stale == []


@pytest.mark.parametrize("flag", ["Running", "Paused", "Restarting"])
def test_active_instances_recovers_an_interrupted_online_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: _lifecycle_info(flag, selected, system=store.system),
    )

    def run(command, **_kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0.0.0.0:4317\n",
            stderr="",
        )

    monkeypatch.setattr(docker, "_run", run)
    recovered: list[Instance] = []

    retained = active_instances(store, docker, recovered=recovered)

    assert [instance.id for instance in retained] == [selected.id]
    assert [instance.id for instance in recovered] == [selected.id]
    persisted = store.load(selected.id)
    assert persisted.active is True
    assert persisted.port == 4317
    assert commands == [
        ["docker", "port", "team-container-id", "4137/tcp"],
    ]


def test_active_instances_preserves_offline_portless_state(
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
        "_inspect_container",
        lambda _name: _lifecycle_info("Running", selected, system=store.system),
    )
    monkeypatch.setattr(
        docker,
        "current_published_port",
        lambda *_args, **_kwargs: pytest.fail(
            "offline instances have no published AgentWS port"
        ),
        raising=False,
    )
    recovered: list[Instance] = []

    retained = active_instances(store, docker, recovered=recovered)

    assert [instance.id for instance in retained] == [selected.id]
    assert recovered == []
    assert store.load(selected.id).port is None


def test_active_instances_rejects_a_foreign_same_name_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    foreign = _lifecycle_info("Paused", selected, system="ba9876543210")
    monkeypatch.setattr(docker, "_inspect_container", lambda _name: foreign)
    stale: list[Instance] = []

    with pytest.raises(CycloError, match="non-Cyclo container"):
        active_instances(store, docker, stale=stale)

    assert store.load(selected.id).active is True
    assert stale == []


def test_stop_refuses_incomplete_inventory_before_any_side_effect(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.port = 4137
    _persist(store, selected)
    metadata_before = store.metadata_path(selected.id).read_bytes()
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not-json\n", encoding="utf-8")
    events: list[str] = []

    class NoDockerWrites:
        def __getattr__(self, name: str):
            def unexpected(*_args, **_kwargs):
                events.append(f"docker:{name}")
                raise AssertionError(f"unexpected Docker operation: {name}")

            return unexpected

    with pytest.raises(CycloError, match="cannot enumerate Cyclo instance state"):
        stop_instance(
            store,
            NoDockerWrites(),  # type: ignore[arg-type]
            selected,
        )

    assert events == []
    assert store.metadata_path(selected.id).read_bytes() == metadata_before


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
    assert persisted.active is False
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

    assert store.load(expected.id).active is True


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_repair_does_not_revoke_or_delete_lifecycle_active_team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    _persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: _lifecycle_info(flag, selected, system=store.system),
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        docker,
        "_run",
        lambda command, **_kwargs: commands.append(list(command))
        or subprocess.CompletedProcess(
            command,
            0,
            stdout="127.0.0.1:4317\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        docker,
        "stop_remove",
        lambda *_args, **_kwargs: pytest.fail(
            "repair must not remove a paused/restarting team"
        ),
    )
    monkeypatch.setattr(
        docker,
        "remove_network",
        lambda *_args, **_kwargs: pytest.fail(
            "repair must not remove a paused/restarting network"
        ),
    )

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    assert cmd_repair(SimpleNamespace()) == 0

    repaired = store.load(selected.id)
    assert repaired.active is True
    assert repaired.port == 4317
    assert commands == [
        ["docker", "port", "team-container-id", "4137/tcp"],
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
        "_inspect_container",
        lambda _name: _lifecycle_info(flag, selected, system=store.system),
    )

    with pytest.raises(CycloError, match=f"already active \\({flag.lower()}\\)"):
        preflight_binding(binding, store, docker)


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
        "_inspect_container",
        lambda name: _lifecycle_info(flag, running, system=store.system)
        if name == running.container_name
        else None,
    )

    with pytest.raises(CycloError, match="overlaps project of running instance"):
        validate_running_mount_boundaries(binding, store, docker)
