from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.cli import cmd_repair
from cyclo.docker import Docker, DockerContainerState
from cyclo.errors import CycloError
from cyclo.instance_lifecycle import active_instances, stop_instance
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
        active=active,
    )


def _lifecycle_info(flag: str) -> dict[str, object]:
    return {"State": {"Running": True, flag: True}}


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("Paused", DockerContainerState.PAUSED),
        ("Restarting", DockerContainerState.RESTARTING),
    ],
)
def test_paused_and_restarting_are_lifecycle_active_but_not_operational(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    expected: DockerContainerState,
) -> None:
    docker = Docker()
    monkeypatch.setattr(
        docker, "_inspect_container", lambda _name: _lifecycle_info(flag)
    )

    assert docker.container_lifecycle_state("cyclo-team") is expected
    assert docker.container_lifecycle_active("cyclo-team") is True
    assert docker.container_running("cyclo-team") is False


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_active_instances_preserves_temporary_lifecycle_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    store.save(selected)
    docker = Docker()
    monkeypatch.setattr(
        docker, "_inspect_container", lambda _name: _lifecycle_info(flag)
    )
    stale: list[Instance] = []

    retained = active_instances(store, docker, stale=stale)

    assert [instance.id for instance in retained] == [selected.id]
    assert store.load(selected.id).active is True
    assert stale == []


def test_stop_refuses_incomplete_inventory_before_any_side_effect(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    selected.port = 4137
    store.save(selected)
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

    class NoRuntimeWrites:
        container_name = "cyclo-provider-runtime-test"

        def __getattr__(self, name: str):
            def unexpected(*_args, **_kwargs):
                events.append(f"runtime:{name}")
                raise AssertionError(f"unexpected runtime operation: {name}")

            return unexpected

    with pytest.raises(CycloError, match="cannot enumerate Cyclo instance state"):
        stop_instance(
            store,
            NoDockerWrites(),  # type: ignore[arg-type]
            NoRuntimeWrites(),  # type: ignore[arg-type]
            selected.id,
        )

    assert events == []
    assert store.metadata_path(selected.id).read_bytes() == metadata_before


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_repair_does_not_revoke_or_delete_lifecycle_active_team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _instance("team", tmp_path / "team", tmp_path / "project")
    store.save(selected)
    docker = Docker()
    monkeypatch.setattr(
        docker, "_inspect_container", lambda _name: _lifecycle_info(flag)
    )
    events: list[object] = []
    monkeypatch.setattr(
        docker,
        "ensure_network",
        lambda name, *, offline: events.append(("network", name, offline))
        or "network-id",
    )
    monkeypatch.setattr(
        docker,
        "connect_runtime",
        lambda network, runtime, alias: events.append(
            ("connect", network, runtime, alias)
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

    class Runtime:
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def status():
            return SimpleNamespace(running=True, container_id="runtime-id")

        @staticmethod
        def update_clients(instances) -> None:
            events.append(("clients", tuple(item.id for item in instances)))

        @staticmethod
        def rotate_client_token(_identifier: str) -> None:
            pytest.fail("repair must not revoke a lifecycle-active team token")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: Runtime()
    )

    assert cmd_repair(SimpleNamespace()) == 0

    assert store.load(selected.id).active is True
    assert ("clients", (selected.id,)) in events


@pytest.mark.parametrize("flag", ["Paused", "Restarting"])
def test_preflight_rejects_duplicate_lifecycle_active_container(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    selected = _instance("team", team_repo, project_repo)
    binding = RunBinding(
        team=load_team(team_repo),
        project_root=project_repo,
        instance=selected,
        manifest="",
    )
    docker = Docker()
    monkeypatch.setattr(
        docker, "_inspect_container", lambda _name: _lifecycle_info(flag)
    )

    with pytest.raises(CycloError, match=f"already active \\({flag.lower()}\\)"):
        preflight_binding(binding, StateStore(tmp_path / "state"), docker)


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
    store.save(running)
    binding = RunBinding(
        team=load_team(team_repo),
        project_root=nested_project,
        instance=_instance("new", team_repo, nested_project),
        manifest="",
        project_mounts=(
            ProjectMount("source", nested_project.resolve(), "rw", 1),
        ),
    )
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda name: _lifecycle_info(flag)
        if name == running.container_name
        else None,
    )

    with pytest.raises(CycloError, match="overlaps project of running instance"):
        validate_running_mount_boundaries(binding, store, docker)
