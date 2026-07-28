from __future__ import annotations

import json
import multiprocessing
import os
import signal
import stat
from pathlib import Path

import pytest

import cyclo.state as state_module
from cyclo.agentws_bundle import packaged_agentws_template
from cyclo.errors import CycloError
from cyclo.installation import team_container_name, team_network_name
from cyclo.state import (
    Instance,
    StateStore,
)


CONTAINER_PROJECT_CONFIG = (
    "name runtime-test\n"
    "description State materialization test.\n"
    "team /team ro\n"
    "mount source /workspace/source rw\n"
    "mount docs /readonly/docs ro\n"
)


def make_instance(identifier: str, store: StateStore) -> Instance:
    return Instance(
        id=identifier,
        team_name="team",
        team_path="/tmp/team",
        project_path="/tmp/project",
        generation="abc",
        providers=["openai-codex"],
        models=["openai-codex/gpt-test"],
        container_name=team_container_name(store.system, identifier),
        network_name=team_network_name(store.system, identifier),
        image="cyclo-runtime:test",
        team_write=False,
        offline=False,
        launch_id="0" * 32,
        provider_socket_path="/tmp/cyclo-provider/component.sock",
        provider_generation="provider-generation",
    )


def test_metadata_round_trip_has_no_token(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance("alpha", store)
    instance.intent = "running"
    store.save(instance)

    loaded = store.load("alpha")
    text = store.metadata_path("alpha").read_text(encoding="utf-8")
    assert loaded == instance
    assert "token" not in text.lower()
    assert '"intent": "running"' in text
    assert '"active"' not in text
    assert store.metadata_path("alpha").stat().st_mode & 0o777 == 0o600


def test_metadata_requires_requested_port(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("legacy-port")
    path.parent.mkdir(parents=True)
    payload = make_instance("legacy-port", store).as_json()
    payload.pop("requested_port")
    payload["port"] = 49152
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match="requested_port is required"):
        store.load("legacy-port")


@pytest.mark.parametrize("value", (-1, 65536, None, True, "8123"))
def test_instance_metadata_rejects_invalid_requested_port(
    tmp_path: Path,
    value: object,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("invalid-requested-port")
    path.parent.mkdir(parents=True)
    payload = make_instance("invalid-requested-port", store).as_json()
    payload["requested_port"] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match="requested_port"):
        store.load("invalid-requested-port")


def test_instance_launch_metadata_is_durable_before_container_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    selected = make_instance("alpha", store)
    synced: list[int] = []
    monkeypatch.setattr(
        state_module.os,
        "fsync",
        lambda descriptor: synced.append(
            stat.S_IFMT(os.fstat(descriptor).st_mode)
        ),
    )

    store.save(selected)
    assert synced == [stat.S_IFREG, stat.S_IFDIR, stat.S_IFDIR]

    synced.clear()
    selected.launch_id = "1" * 32
    store.save(selected)
    assert synced == [stat.S_IFREG, stat.S_IFDIR]


def test_first_state_directory_publication_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "new-parent" / "state")
    synced: list[Path] = []
    monkeypatch.setattr(
        store,
        "_sync_directory",
        lambda path: synced.append(path),
    )

    store.ensure()

    ancestry = [store.root]
    while ancestry[-1] != ancestry[-1].parent:
        ancestry.append(ancestry[-1].parent)
    expected = [
        *ancestry,
        store.instances_dir,
        store.deletions_dir,
        store.components_root,
        store.root,
    ]
    assert synced == expected
    synced.clear()
    store.ensure()
    assert synced == []


def test_state_directory_sync_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "new-parent" / "state")
    attempts = 0
    synced: list[Path] = []

    def fail_once(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected directory sync failure")
        synced.append(path)

    monkeypatch.setattr(store, "_sync_directory", fail_once)

    with pytest.raises(CycloError, match="injected directory sync failure"):
        store.ensure()
    store.ensure()

    assert synced[:3] == [
        store.root,
        store.root.parent,
        store.root.parent.parent,
    ]
    assert store.instances_dir in synced
    assert store.deletions_dir in synced
    assert store.components_root in synced


def test_host_configuration_scope_is_synced_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(
        tmp_path / "state",
        requested_host_config_scope="local",
    )
    store.ensure()
    _ = store.host_config_scope
    synced: list[int] = []
    monkeypatch.setattr(
        state_module.os,
        "fsync",
        lambda descriptor: synced.append(
            stat.S_IFMT(os.fstat(descriptor).st_mode)
        ),
    )

    with store.locked():
        pass

    assert synced == [stat.S_IFREG, stat.S_IFDIR]


def test_metadata_rejects_legacy_active(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("legacy")
    path.parent.mkdir(parents=True)
    payload = make_instance("legacy", store).as_json()
    payload.pop("intent")
    payload["active"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match="active is not supported"):
        store.load("legacy")


@pytest.mark.parametrize("value", ("active", "", None, True))
def test_instance_metadata_rejects_invalid_intent(
    tmp_path: Path,
    value: object,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("invalid-intent")
    path.parent.mkdir(parents=True)
    payload = make_instance("invalid-intent", store).as_json()
    payload["intent"] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match="intent must"):
        store.load("invalid-intent")


def test_sigkill_during_first_metadata_publication_cannot_poison_inventory(
    tmp_path: Path,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("requires fork to stop a save at the publication boundary")

    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    ready = context.Event()

    def save_until_killed() -> None:
        real_rename = state_module.os.rename

        def block_publication(source, destination) -> None:
            if Path(destination) == store.instance_dir("alpha"):
                ready.set()
                signal.pause()
            real_rename(source, destination)

        state_module.os.rename = block_publication
        store.save(selected)

    process = context.Process(target=save_until_killed)
    process.start()
    try:
        assert ready.wait(5), "child did not reach metadata publication"
        process.kill()
        process.join(5)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)

    assert not store.instance_dir("alpha").exists()
    assert store.list() == []


def test_sigkill_during_instance_purge_is_discoverable_and_retryable(
    tmp_path: Path,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("requires fork to stop retirement after its atomic rename")

    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    task = store.tasks_dir("alpha") / "task"
    task.mkdir(parents=True)
    (task / "spec.md").write_text("durable work\n", encoding="utf-8")
    ready = context.Event()

    def retire_until_killed() -> None:
        real_rmtree = state_module.shutil.rmtree

        def block_removal(path, *args, **kwargs) -> None:
            if Path(path) == store.deletion_dir("alpha") / "agentws-state":
                ready.set()
                signal.pause()
            real_rmtree(path, *args, **kwargs)

        state_module.shutil.rmtree = block_removal
        store.remove_instance(
            selected.id,
            expected_launch_id=selected.launch_id,
        )

    process = context.Process(target=retire_until_killed)
    process.start()
    try:
        assert ready.wait(5), "child did not reach retired-state cleanup"
        process.kill()
        process.join(5)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)

    assert not store.instance_dir("alpha").exists()
    assert store.deletion_dir("alpha").is_dir()
    assert store.list() == []
    assert [item.id for item in store.list_deletions()] == ["alpha"]
    deleting = store.load_for_removal("alpha")
    assert deleting.intent == "deleting"
    assert deleting.launch_id == selected.launch_id

    assert store.remove_instance(
        "alpha",
        expected_launch_id=selected.launch_id,
    )
    assert not store.deletion_dir("alpha").exists()
    with pytest.raises(CycloError, match="not found"):
        store.load_for_removal("alpha")


def test_persisted_instance_requires_a_launch_identity(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    selected.launch_id = ""

    with pytest.raises(CycloError, match="launch_id must be"):
        store.save(selected)


def test_remove_instance_deletes_its_complete_durable_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    task = store.tasks_dir("alpha") / "task"
    task.mkdir(parents=True)
    (task / "spec.md").write_text("durable work\n", encoding="utf-8")

    assert store.remove_instance(
        "alpha",
        expected_launch_id=selected.launch_id,
    )
    assert not store.instance_dir("alpha").exists()
    assert not store.deletion_dir("alpha").exists()
    assert not store.remove_instance(
        "alpha",
        expected_launch_id=selected.launch_id,
    )


def test_remove_instance_persists_deleting_before_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    real_rename = state_module.os.rename

    def fail_retirement(source, destination) -> None:
        if (
            Path(source) == store.instance_dir("alpha")
            and Path(destination) == store.deletion_dir("alpha")
        ):
            persisted = store.load("alpha")
            assert persisted.intent == "deleting"
            raise OSError("injected retirement failure")
        real_rename(source, destination)

    monkeypatch.setattr(state_module.os, "rename", fail_retirement)

    with pytest.raises(CycloError, match="injected retirement failure"):
        store.remove_instance(
            "alpha",
            expected_launch_id=selected.launch_id,
        )

    persisted = store.load_for_removal("alpha")
    assert persisted.intent == "deleting"
    assert persisted.port is None
    assert not store.deletion_dir("alpha").exists()


def test_retirement_sync_failure_keeps_destination_record_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    deletion = store.deletion_dir(selected.id)
    real_sync = store._sync_directory

    def fail_after_destination(path: Path) -> None:
        real_sync(path)
        if path == store.deletions_dir and deletion.exists():
            raise OSError("injected power boundary")

    monkeypatch.setattr(store, "_sync_directory", fail_after_destination)

    with pytest.raises(CycloError, match="injected power boundary"):
        store.remove_instance(
            selected.id,
            expected_launch_id=selected.launch_id,
        )

    pending = store.load_for_removal(selected.id)
    assert pending is not None
    assert pending.intent == "deleting"
    assert deletion.is_dir()


def test_remove_instance_retry_from_deletion_is_exact_launch_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    state = store.queue_root("alpha") / "durable"
    state.mkdir(parents=True)
    real_rmtree = state_module.shutil.rmtree

    def fail_purge(path, *args, **kwargs) -> None:
        if Path(path) == store.deletion_dir("alpha") / "agentws-state":
            raise OSError("injected purge failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(state_module.shutil, "rmtree", fail_purge)
    with pytest.raises(CycloError, match="injected purge failure"):
        store.remove_instance(
            "alpha",
            expected_launch_id=selected.launch_id,
        )
    monkeypatch.setattr(state_module.shutil, "rmtree", real_rmtree)

    with pytest.raises(CycloError, match="was replaced"):
        store.remove_instance(
            "alpha",
            expected_launch_id="1" * 32,
        )

    pending = store.load_for_removal("alpha")
    assert pending.intent == "deleting"
    assert pending.launch_id == selected.launch_id
    assert store.list_deletions() == [pending]
    assert store.remove_instance(
        "alpha",
        expected_launch_id=selected.launch_id,
    )
    assert not store.deletion_dir("alpha").exists()


def test_pending_deletion_blocks_instance_name_reuse(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    selected.intent = "deleting"
    store.save(selected)
    os.rename(store.instance_dir("alpha"), store.deletion_dir("alpha"))

    replacement = make_instance("alpha", store)
    replacement.launch_id = "1" * 32
    with pytest.raises(CycloError, match="deletion is still pending"):
        store.save(replacement)


def test_remove_instance_retries_a_finalized_exact_launch_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    finalized = store._final_deletion_path(
        selected.id, selected.launch_id
    )
    real_unlink = Path.unlink

    def fail_final_purge(path: Path, *args, **kwargs) -> None:
        if path == finalized:
            raise OSError("injected tombstone purge failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_final_purge)
    with pytest.raises(CycloError, match="injected tombstone purge failure"):
        store.remove_instance(
            selected.id,
            expected_launch_id=selected.launch_id,
        )
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert not store.instance_dir(selected.id).exists()
    assert not store.deletion_dir(selected.id).exists()
    assert finalized.is_file()
    assert store.list() == []
    deleting = store.load_for_removal(selected.id)
    assert store.list_deletions() == [deleting]
    replacement = make_instance(selected.id, store)
    replacement.launch_id = "1" * 32
    with pytest.raises(CycloError, match="deletion is still pending"):
        store.save(replacement)
    with pytest.raises(CycloError, match="was replaced"):
        store.remove_instance(
            selected.id,
            expected_launch_id="1" * 32,
        )
    assert store.remove_instance(
        selected.id,
        expected_launch_id=selected.launch_id,
    )
    assert not finalized.exists()


def test_sigkill_after_finalizing_deletion_leaves_retryable_tombstone(
    tmp_path: Path,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("requires fork to stop deletion at its final boundary")

    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    finalized = store._final_deletion_path(
        selected.id,
        selected.launch_id,
    )
    ready = context.Event()

    def delete_until_killed() -> None:
        real_rmdir = state_module.os.rmdir

        def block_empty_directory_removal(path, *args, **kwargs) -> None:
            if Path(path) == store.deletion_dir(selected.id):
                ready.set()
                signal.pause()
            real_rmdir(path, *args, **kwargs)

        state_module.os.rmdir = block_empty_directory_removal
        store.remove_instance(
            selected.id,
            expected_launch_id=selected.launch_id,
        )

    process = context.Process(target=delete_until_killed)
    process.start()
    try:
        assert ready.wait(5), "child did not finalize the deletion marker"
        process.kill()
        process.join(5)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)

    assert not store.instance_dir(selected.id).exists()
    assert store.deletion_dir(selected.id).is_dir()
    assert list(store.deletion_dir(selected.id).iterdir()) == []
    assert finalized.is_file()
    deleting = store.load_for_removal(selected.id)
    assert deleting.intent == "deleting"
    assert deleting.launch_id == selected.launch_id
    assert store.list_deletions() == [deleting]

    replacement = make_instance(selected.id, store)
    replacement.launch_id = "1" * 32
    with pytest.raises(CycloError, match="deletion is still pending"):
        store.save(replacement)

    assert store.remove_instance(
        selected.id,
        expected_launch_id=selected.launch_id,
    )
    assert not store.deletion_dir(selected.id).exists()
    assert not finalized.exists()


def test_finalization_sync_failure_keeps_marker_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)
    finalized = store._final_deletion_path(
        selected.id,
        selected.launch_id,
    )
    real_sync = store._sync_directory

    def fail_after_destination(path: Path) -> None:
        real_sync(path)
        if path == store.deletions_dir and finalized.exists():
            raise OSError("injected final power boundary")

    monkeypatch.setattr(store, "_sync_directory", fail_after_destination)

    with pytest.raises(CycloError, match="injected final power boundary"):
        store.remove_instance(
            selected.id,
            expected_launch_id=selected.launch_id,
        )

    deleting = store.load_for_removal(selected.id)
    assert deleting is not None
    assert deleting.launch_id == selected.launch_id
    assert store.deletion_dir(selected.id).is_dir()
    assert finalized.is_file()


def test_remove_instance_rejects_invalid_or_replaced_launch_identity(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    store.save(selected)

    with pytest.raises(CycloError, match="invalid launch identity"):
        store.remove_instance("alpha", expected_launch_id="")
    with pytest.raises(CycloError, match="was replaced"):
        store.remove_instance(
            "alpha",
            expected_launch_id="1" * 32,
        )

    assert store.load("alpha").launch_id == selected.launch_id


def test_state_rejects_namespaced_resources_from_another_installation(
    tmp_path: Path,
) -> None:
    first = StateStore(tmp_path / "first")
    second = StateStore(tmp_path / "second")
    selected = make_instance("alpha", first)
    selected.container_name = team_container_name(first.system, selected.id)
    selected.network_name = team_network_name(first.system, selected.id)

    with pytest.raises(CycloError, match="belongs to another Cyclo installation"):
        second.save(selected)

    first.save(selected)
    assert first.load("alpha").container_name == selected.container_name


def test_host_configuration_scope_binding_is_atomic_and_conflict_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    system = StateStore(root, requested_host_config_scope="system")
    local = StateStore(root, requested_host_config_scope="local")

    assert system.host_config_scope == "system"
    assert local.host_config_scope == "local"
    with system.locked():
        pass

    assert system.host_config_scope_path.read_text(encoding="ascii") == "system\n"
    assert system.host_config_scope_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CycloError, match="retry the command"):
        with local.locked():
            pass

    # A new process adopts the existing binding, regardless of how the same
    # canonical state root was selected.
    reopened = StateStore(root, requested_host_config_scope="local")
    assert reopened.host_config_scope == "system"
    with reopened.locked():
        pass


def test_nonblocking_state_lock_reports_a_busy_installation(
    tmp_path: Path,
) -> None:
    owner = StateStore(tmp_path / "state")
    observer = StateStore(tmp_path / "state")

    with owner.locked():
        with pytest.raises(CycloError, match="state is busy"):
            with observer.locked(blocking=False):
                pytest.fail("acquired an installation lock held elsewhere")


def test_state_lock_does_not_translate_operation_oserror(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    failure = OSError("operation failed")

    with pytest.raises(OSError) as raised:
        with store.locked():
            raise failure

    assert raised.value is failure


@pytest.mark.parametrize("content", ("elsewhere\n", "state\n"))
def test_host_configuration_scope_rejects_invalid_value(
    tmp_path: Path,
    content: str,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    binding = root / "host-config.scope"
    binding.write_text(content, encoding="ascii")
    binding.chmod(0o600)

    store = StateStore(root, requested_host_config_scope="system")
    with pytest.raises(CycloError, match="invalid host configuration scope"):
        _ = store.host_config_scope


def test_host_configuration_scope_rejects_public_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    binding = root / "host-config.scope"
    binding.write_text("system\n", encoding="ascii")
    binding.chmod(0o644)

    store = StateStore(root, requested_host_config_scope="system")
    with pytest.raises(CycloError, match="not private"):
        _ = store.host_config_scope


def test_host_configuration_scope_rejects_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.write_text("system\n", encoding="ascii")
    target.chmod(0o600)
    (root / "host-config.scope").symlink_to(target)

    store = StateStore(root, requested_host_config_scope="system")
    with pytest.raises(CycloError, match="cannot read host configuration scope"):
        _ = store.host_config_scope


def test_state_lock_does_not_bind_an_unused_provider_configuration(
    tmp_path: Path,
) -> None:
    store = StateStore(
        tmp_path / "state",
        requested_host_config_scope="system",
    )

    with store.locked():
        pass

    assert not store.host_config_scope_path.exists()


def test_state_rejects_pre_0_2_team_resource_names(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    selected = make_instance("alpha", store)
    payload = selected.as_json()
    payload["container_name"] = "cyclo-alpha"
    payload["network_name"] = "cyclo-alpha-net"
    path = store.metadata_path("alpha")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match="container_name must be a Cyclo team resource"):
        store.load("alpha")


def test_instance_enumeration_reports_bad_records_and_strict_list_refuses_them(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(make_instance("alpha", store))
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


def test_instance_listing_ignores_deletions_but_validates_their_root(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    malformed = store.deletions_dir / "malformed"
    malformed.mkdir()

    assert store.list() == []
    with pytest.raises(CycloError, match="cannot enumerate Cyclo deletion state"):
        store.list_deletions()

    malformed.rmdir()
    store.deletions_dir.rmdir()
    store.deletions_dir.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(CycloError, match="invalid Cyclo deletions directory"):
        store.list()
    with pytest.raises(CycloError, match="invalid Cyclo deletions directory"):
        store.list_deletions()


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
    outside.write_text(
        json.dumps(make_instance("linked", store).as_json()), encoding="utf-8"
    )
    linked = store.metadata_path("linked")
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)

    with pytest.raises(CycloError, match="cannot read Cyclo instance"):
        store.load("linked")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("verbose", "false", "verbose must be a boolean"),
        ("providers", "openai", "providers must be a list of strings"),
        ("port", True, "port must be null or an integer"),
        ("project_path", {}, "project_path must be a string"),
        ("image_override", [], "image_override must be a string"),
        ("project_name", [], "project_name must be a string"),
        ("project_file", 3, "project_file must be a string"),
        (
            "project_description",
            None,
            "project_description must be a string",
        ),
        ("project_generation", False, "project_generation must be a string"),
        (
            "provider_socket_path",
            [],
            "provider_socket_path must be a string",
        ),
        (
            "provider_generation",
            None,
            "provider_generation must be a string",
        ),
        (
            "container_name",
            "cyclo-someone-else",
            "container_name must be a Cyclo team resource",
        ),
        (
            "network_name",
            "cyclo-someone-else-net",
            "network_name must match the Cyclo team container",
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
    payload = make_instance("wrong-type", store).as_json()
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match=message):
        store.load("wrong-type")
    instances, errors = store.list_report()
    assert instances == []
    assert len(errors) == 1
    assert message in errors[0]


@pytest.mark.parametrize(
    ("project_mounts", "message"),
    [
        (None, "project_mounts must be a list"),
        ([None], r"project_mounts\[0\] must be an object"),
        (
            [{"name": "../escape", "path": "/tmp/project", "mode": "rw"}],
            r"project_mounts\[0\] has an invalid name",
        ),
        (
            [{"name": "source", "path": "relative", "mode": "rw"}],
            r"project_mounts\[0\] has an invalid host path",
        ),
        (
            [{"name": "source", "path": "/tmp/project", "mode": "write"}],
            r"project_mounts\[0\] has an invalid mode",
        ),
    ],
)
def test_instance_metadata_rejects_invalid_persisted_project_mounts(
    tmp_path: Path,
    project_mounts: object,
    message: str,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("invalid-mounts")
    path.parent.mkdir(parents=True)
    payload = make_instance("invalid-mounts", store).as_json()
    payload.update(
        {
            "project_name": "project",
            "project_file": "/tmp/project.cyclo",
            "project_description": "Project description",
            "project_generation": "project-generation",
            "project_mounts": project_mounts,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match=message):
        store.load("invalid-mounts")
    instances, errors = store.list_report()
    assert instances == []
    assert len(errors) == 1
    assert message.replace(r"\[", "[").replace(r"\]", "]") in errors[0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_path", "relative", "project_path must be absolute"),
        ("project_file", "relative.cyclo", "project_file must be absolute"),
        (
            "project_mounts",
            [],
            "project_mounts must contain at least one mount",
        ),
    ],
)
def test_instance_metadata_rejects_inconsistent_persisted_project_state(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("bad-project")
    path.parent.mkdir(parents=True)
    payload = make_instance("bad-project", store).as_json()
    payload.update(
        {
            "project_name": "project",
            "project_file": "/tmp/project.cyclo",
            "project_description": "Project description",
            "project_generation": "project-generation",
            "project_mounts": [
                {"name": "source", "path": "/tmp/project", "mode": "rw"}
            ],
        }
    )
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match=message):
        store.load("bad-project")


def test_instance_metadata_rejects_relative_provider_socket(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("bad-provider-socket")
    path.parent.mkdir(parents=True)
    payload = make_instance("bad-provider-socket", store).as_json()
    payload["provider_socket_path"] = "relative/component.sock"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        CycloError, match="provider_socket_path must be empty or absolute"
    ):
        store.load("bad-provider-socket")


@pytest.mark.parametrize(
    ("path_value", "generation", "message"),
    [
        ("/tmp/provider/not-a-socket", "generation", "must end in component.sock"),
        (
            "/tmp/provider/component.sock",
            "",
            "provider_socket_path and provider_generation must be set together",
        ),
        (
            "",
            "generation",
            "provider_socket_path and provider_generation must be set together",
        ),
    ],
)
def test_instance_metadata_rejects_inconsistent_provider_endpoint(
    tmp_path: Path,
    path_value: str,
    generation: str,
    message: str,
) -> None:
    store = StateStore(tmp_path / "state")
    path = store.metadata_path("bad-provider-endpoint")
    path.parent.mkdir(parents=True)
    payload = make_instance("bad-provider-endpoint", store).as_json()
    payload["provider_socket_path"] = path_value
    payload["provider_generation"] = generation
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CycloError, match=message):
        store.load("bad-provider-endpoint")


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
    instance = make_instance("alpha", store)
    instance.network_name = "cyclo-another-instance-net"

    with pytest.raises(CycloError, match="network_name must match the Cyclo team container"):
        store.save(instance)

    assert not store.metadata_path("alpha").exists()

    instance = make_instance("bad-id", store)
    instance.id = 3  # type: ignore[assignment]
    with pytest.raises(CycloError, match="invalid Cyclo instance ID 3"):
        store.save(instance)


def test_materialize_agentws_preserves_queue_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    runtime = store.materialize_agentws(
        "alpha",
        packaged_agentws_template(),
        script,
        project_config=CONTAINER_PROJECT_CONFIG,
    )
    marker = store.tasks_dir("alpha") / "existing"
    marker.mkdir()
    (marker / "state").write_text("open\n", encoding="utf-8")
    updated_config = CONTAINER_PROJECT_CONFIG.replace(
        "State materialization test.",
        "Updated state materialization test.",
    )

    store.materialize_agentws(
        "alpha",
        packaged_agentws_template(),
        script,
        project_config=updated_config,
    )

    assert (marker / "state").read_text(encoding="utf-8") == "open\n"
    assert (runtime / "tools" / "run_agentws").is_file()
    assert (runtime / ".cyclo-runtime.py").is_file()
    project_config = runtime / "project.cyclo"
    assert project_config.read_text(encoding="utf-8") == updated_config
    assert project_config.stat().st_mode & 0o777 == 0o444


def test_materialize_agentws_requires_project_config(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"

    with pytest.raises(
        CycloError,
        match="project configuration must not be empty",
    ):
        store.materialize_agentws(
            "alpha",
            packaged_agentws_template(),
            script,
            project_config=" \n",
        )

    assert not store.runtime_root("alpha").exists()


def test_materialized_project_config_and_workspace_layout_are_replaced_safely(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    runtime = store.materialize_agentws(
        "alpha",
        packaged_agentws_template(),
        script,
        project_config=CONTAINER_PROJECT_CONFIG,
    )
    layout = store.materialize_workspace_layout("alpha", ["source"])
    readonly_layout = store.materialize_readonly_layout("alpha", ["docs"])

    project_config = runtime / "project.cyclo"
    assert project_config.read_text(encoding="utf-8") == CONTAINER_PROJECT_CONFIG
    assert project_config.stat().st_mode & 0o777 == 0o444
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
        store.save(make_instance("alpha", store))

    assert not (outside / "run.json").exists()


@pytest.mark.parametrize("metadata_kind", ("missing", "symlink", "fifo"))
def test_save_never_adopts_incomplete_instance_state(
    tmp_path: Path,
    metadata_kind: str,
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    directory = store.instance_dir("alpha")
    directory.mkdir()
    sentinel = directory / "foreign-state"
    sentinel.write_text("untouched\n", encoding="utf-8")
    metadata = directory / "run.json"
    if metadata_kind == "symlink":
        target = tmp_path / "outside"
        target.write_text("outside\n", encoding="utf-8")
        metadata.symlink_to(target)
    elif metadata_kind == "fifo":
        os.mkfifo(metadata)

    with pytest.raises(CycloError, match="Cyclo instance"):
        store.save(make_instance("alpha", store))

    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    if metadata_kind == "missing":
        assert not metadata.exists()
    elif metadata_kind == "symlink":
        assert metadata.is_symlink()
        assert (tmp_path / "outside").read_text(encoding="utf-8") == "outside\n"
    else:
        assert metadata.is_fifo()


def test_rematerialize_does_not_follow_persisted_symlink(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    runtime = store.materialize_agentws(
        "alpha",
        packaged_agentws_template(),
        script,
        project_config=CONTAINER_PROJECT_CONFIG,
    )
    target = tmp_path / "host-target"
    target.write_text("unchanged\n", encoding="utf-8")
    runtime_script = runtime / ".cyclo-runtime.py"
    runtime_script.unlink()
    runtime_script.symlink_to(target)

    store.materialize_agentws(
        "alpha",
        packaged_agentws_template(),
        script,
        project_config=CONTAINER_PROJECT_CONFIG,
    )

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
