from __future__ import annotations

import json
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


def test_instance_enumeration_reports_bad_records_and_strict_list_refuses_them(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(make_instance("alpha"))
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not-json\n", encoding="utf-8")
    undecodable = store.metadata_path("undecodable")
    undecodable.parent.mkdir(parents=True)
    undecodable.write_bytes(b"\xff")

    instances, errors = store.list_report()

    assert [instance.id for instance in instances] == ["alpha"]
    assert len(errors) == 2
    assert any(str(broken) in error for error in errors)
    assert any(str(undecodable) in error for error in errors)
    assert all("invalid Cyclo instance metadata" in error for error in errors)
    with pytest.raises(CycloError, match="cannot enumerate Cyclo instance state"):
        store.list()


def test_direct_instance_load_rejects_undecodable_and_symlinked_metadata(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    undecodable = store.metadata_path("undecodable")
    undecodable.parent.mkdir(parents=True)
    undecodable.write_bytes(b"\xff")

    with pytest.raises(CycloError, match="cannot read Cyclo instance"):
        store.load("undecodable")

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(make_instance("linked").as_json()), encoding="utf-8")
    linked = store.metadata_path("linked")
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)

    with pytest.raises(CycloError, match="cannot read Cyclo instance"):
        store.load("linked")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("active", "false", "active must be a boolean"),
        ("providers", "openai", "providers must be a list of strings"),
        ("port", True, "port must be null or an integer"),
        (
            "container_name",
            "cyclo-someone-else",
            "container_name must be 'cyclo-wrong-type'",
        ),
        (
            "network_name",
            "cyclo-someone-else-net",
            "network_name must be 'cyclo-wrong-type-net'",
        ),
    ],
)
def test_instance_metadata_rejects_wrong_field_types(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("wrong-type")
    path.parent.mkdir(parents=True)
    payload = make_instance("wrong-type").as_json()
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match=message):
        store.load("wrong-type")
    instances, errors = store.list_report()
    assert instances == []
    assert len(errors) == 1
    assert message in errors[0]


def test_instance_metadata_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("fifo")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    instances, errors = store.list_report()

    assert instances == []
    assert len(errors) == 1
    assert str(path) in errors[0]
    assert "not a regular file" in errors[0]


def test_deeply_nested_instance_metadata_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("nested")
    path.parent.mkdir(parents=True)
    path.write_text(
        "{\"x\":" + "[" * 2000 + "0" + "]" * 2000 + "}",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="cannot read Cyclo instance"):
        store.load("nested")
    instances, errors = store.list_report()
    assert instances == []
    assert len(errors) == 1
    assert str(path) in errors[0]


def test_save_refuses_state_that_strict_reader_would_reject(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance("alpha")
    instance.network_name = "cyclo-another-instance-net"

    with pytest.raises(CycloError, match="network_name must be 'cyclo-alpha-net'"):
        store.save(instance)

    assert not store.metadata_path("alpha").exists()

    instance = make_instance("bad-id")
    instance.id = 3  # type: ignore[assignment]
    with pytest.raises(CycloError, match="invalid Cyclo instance ID 3"):
        store.save(instance)


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


def test_materialized_project_manifest_and_workspace_layout_are_replaced_safely(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    runtime = store.materialize_agentws(
        "alpha",
        packaged_agentws_template(),
        script,
        project_manifest="# Project\n\n- /workspace/source (read-write)\n",
    )
    layout = store.materialize_workspace_layout("alpha", ["source"])
    readonly_layout = store.materialize_readonly_layout("alpha", ["docs"])

    assert (runtime / "PROJECT.md").read_text(encoding="utf-8").startswith(
        "# Project"
    )
    assert (runtime / "PROJECT.md").stat().st_mode & 0o777 == 0o444
    assert sorted(path.name for path in layout.iterdir()) == ["source"]
    assert sorted(path.name for path in readonly_layout.iterdir()) == ["docs"]

    outside = tmp_path / "outside"
    outside.mkdir()
    (layout / "source").rmdir()
    (layout / "source").symlink_to(outside, target_is_directory=True)
    store.materialize_workspace_layout("alpha", ["replacement"])
    store.materialize_readonly_layout("alpha", ["references"])

    assert outside.is_dir()
    assert not (store.workspace_root("alpha") / "source").exists()
    assert (store.workspace_root("alpha") / "replacement").is_dir()
    assert not (store.readonly_root("alpha") / "docs").exists()
    assert (store.readonly_root("alpha") / "references").is_dir()


def test_workspace_layout_rejects_path_like_mount_names(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")

    with pytest.raises(CycloError, match="invalid Cyclo instance ID"):
        store.materialize_workspace_layout("alpha", ["../escape"])
    with pytest.raises(CycloError, match="invalid Cyclo instance ID"):
        store.materialize_readonly_layout("alpha", ["../escape"])

    assert not (store.instance_dir("alpha") / "escape").exists()


def test_old_instance_metadata_loads_with_empty_project_definition_fields(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance("legacy")
    payload = instance.as_json()
    payload["project_read_only"] = True
    for key in (
        "project_name",
        "project_file",
        "project_description",
        "project_generation",
        "project_mounts",
        "launch_id",
    ):
        payload.pop(key)
    path = store.metadata_path("legacy")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load("legacy")

    assert loaded.project_file == ""
    assert loaded.project_mounts == []
    assert loaded.legacy_project_read_only is True
    assert loaded.as_json()["project_read_only"] is True
    assert "legacy_project_read_only" not in loaded.as_json()


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
