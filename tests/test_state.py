from __future__ import annotations

import os
from pathlib import Path

import pytest

from cyclo.agentws_bundle import packaged_agentws_template
from cyclo.errors import CycloError
from cyclo.state import Instance, StateStore, instance_id


def make_instance(identifier: str) -> Instance:
    return Instance(
        id=identifier,
        team_name="team",
        team_path="/tmp/team",
        project_path="/tmp/project",
        generation="abc",
        providers=["openai-codex"],
        models=["openai-codex/gpt-test"],
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
    )


def test_instance_id_is_stable_and_path_specific(tmp_path: Path) -> None:
    team = tmp_path / "team"
    one = tmp_path / "one"
    two = tmp_path / "two"

    assert instance_id(team, one) == instance_id(team, one)
    assert instance_id(team, one) != instance_id(team, two)


def test_metadata_round_trip_has_no_token(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance("alpha")
    instance.active = True
    store.save(instance)

    loaded = store.load("alpha")
    text = store.metadata_path("alpha").read_text(encoding="utf-8")
    assert loaded == instance
    assert "token" not in text.lower()
    assert store.metadata_path("alpha").stat().st_mode & 0o777 == 0o600


def test_materialize_agentws_preserves_queue_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    runtime = store.materialize_agentws("alpha", packaged_agentws_template(), script)
    marker = store.tasks_dir("alpha") / "existing"
    marker.mkdir()
    (marker / "state").write_text("open\n", encoding="utf-8")

    store.materialize_agentws("alpha", packaged_agentws_template(), script)

    assert (marker / "state").read_text(encoding="utf-8") == "open\n"
    assert (runtime / "tools" / "run_agentws").is_file()
    assert (runtime / ".cyclo-runtime.py").is_file()


def test_instance_paths_reject_traversal(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")

    for identifier in ("../escape", "../../escape", "/tmp/escape", ".", ".."):
        try:
            store.metadata_path(identifier)
        except Exception:
            pass
        else:
            raise AssertionError(f"accepted unsafe instance ID: {identifier}")


def test_save_rejects_symlinked_instance_directory(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.instances_dir / "alpha").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CycloError, match="symlinked Cyclo instance directory"):
        store.save(make_instance("alpha"))

    assert not (outside / "run.json").exists()


def test_rematerialize_does_not_follow_persisted_symlink(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    runtime = store.materialize_agentws("alpha", packaged_agentws_template(), script)
    target = tmp_path / "host-target"
    target.write_text("unchanged\n", encoding="utf-8")
    runtime_script = runtime / ".cyclo-runtime.py"
    runtime_script.unlink()
    runtime_script.symlink_to(target)

    store.materialize_agentws("alpha", packaged_agentws_template(), script)

    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not (store.runtime_root("alpha") / ".cyclo-runtime.py").is_symlink()


def test_failed_tree_install_and_restore_preserves_quarantine(
    tmp_path: Path, monkeypatch
) -> None:
    store = StateStore(tmp_path / "state")
    target = tmp_path / "target"
    target.mkdir()
    (target / "old").write_text("preserve me\n", encoding="utf-8")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    (temporary / "new").write_text("new tree\n", encoding="utf-8")
    real_replace = os.replace
    quarantine: list[Path] = []

    def injected_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == target:
            quarantine.append(destination_path)
            return real_replace(source_path, destination_path)
        if source_path == temporary:
            raise OSError("injected install failure")
        if quarantine and source_path == quarantine[0]:
            raise OSError("injected restore failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr("cyclo.state.os.replace", injected_replace)

    with pytest.raises(CycloError, match="previous tree is preserved"):
        store.replace_tree(temporary, target)

    assert len(quarantine) == 1
    assert not target.exists()
    assert (quarantine[0] / "old").read_text(encoding="utf-8") == "preserve me\n"
    assert not temporary.exists()
