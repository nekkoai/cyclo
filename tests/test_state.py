from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from cyclo.errors import CycloError
from cyclo.state import Instance, StateStore, validate_instance_id


PROJECT_CONFIG = (
    "name state-test\n"
    "description State test.\n"
    "team /team ro\n"
    "mount source /workspace/source rw\n"
)


def make_instance(tmp_path: Path, identifier: str = "alpha") -> Instance:
    return Instance(
        id=identifier,
        team_name="team",
        team_path=str(tmp_path / "team"),
        generation="team-generation",
        models=["openai-codex/model"],
        image="sha256:" + "a" * 64,
        image_override="",
        team_write=False,
        offline=False,
        verbose=False,
        agentws_host="127.0.0.1",
        intent="running",
        requested_port=0,
        team_roster="team",
        team_protocol=False,
        pi_default_provider="openai-codex",
        pi_default_model="model",
        project_name="state-test",
        project_file=str(tmp_path / "project.cyclo"),
        project_description="State test.",
        project_generation="project-generation",
        project_config=PROJECT_CONFIG,
        project_mounts=[
            {
                "name": "source",
                "path": str(tmp_path / "source"),
                "mode": "rw",
            }
        ],
        runtime_version="0.2.0",
    )


def test_state_round_trip_contains_domain_intent_only(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance(tmp_path)

    store.save(instance)

    loaded = store.load(instance.id)
    document = json.loads(
        store.metadata_path(instance.id).read_text(encoding="utf-8")
    )
    assert loaded == instance
    assert document["intent"] == "running"
    assert document["image"].startswith("sha256:")
    for legacy in (
        "container_name",
        "network_name",
        "launch_id",
        "provider_socket_path",
        "provider_generation",
    ):
        assert legacy not in document
    assert stat.S_IMODE(store.metadata_path(instance.id).stat().st_mode) == 0o600


def test_legacy_lifecycle_fields_are_rejected(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    document = instance.as_json()
    document["container_name"] = "legacy"

    with pytest.raises(TypeError, match="unknown state field"):
        Instance.from_json(document)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("image", "cyclo-team:mutable", "immutable"),
        ("intent", "active", "running or stopped"),
        ("requested_port", 65536, "requested_port"),
        ("project_mounts", [], "at least one mount"),
        ("models", [], "models"),
    ),
)
def test_invalid_domain_state_fails_before_persistence(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance(tmp_path)
    setattr(instance, field, value)

    with pytest.raises(CycloError, match=message):
        store.save(instance)
    assert not store.metadata_path(instance.id).exists()


def test_inventory_reports_corrupt_committed_entries(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save(make_instance(tmp_path, "good"))
    bad = store.instance_dir("bad")
    bad.mkdir()
    (bad / "run.json").write_text("{", encoding="utf-8")

    instances, errors = store.list_report()

    assert [instance.id for instance in instances] == ["good"]
    assert len(errors) == 1
    assert "bad" in errors[0]
    with pytest.raises(CycloError, match="cannot enumerate"):
        store.list()


def test_empty_first_publication_directory_is_retryable(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    store.instance_dir("alpha").mkdir()

    assert store.list_report() == ([], [])
    store.save(make_instance(tmp_path))
    assert store.load("alpha").id == "alpha"


@pytest.mark.parametrize("existing_kind", ("symlink", "fifo", "invalid", "wrong-id"))
def test_save_refuses_invalid_existing_instance_document(
    tmp_path: Path,
    existing_kind: str,
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    directory = store.instance_dir("alpha")
    directory.mkdir()
    path = store.metadata_path("alpha")
    if existing_kind == "symlink":
        target = tmp_path / "outside-run.json"
        target.write_text("{}\n", encoding="utf-8")
        path.symlink_to(target)
    elif existing_kind == "fifo":
        os.mkfifo(path)
    elif existing_kind == "invalid":
        path.write_text("{\n", encoding="utf-8")
    else:
        path.write_text(
            json.dumps(make_instance(tmp_path, "beta").as_json()) + "\n",
            encoding="utf-8",
        )
    original_inode = path.lstat().st_ino

    with pytest.raises(
        CycloError,
        match="invalid existing Cyclo instance state|ID mismatch",
    ):
        store.save(make_instance(tmp_path, "alpha"))

    assert path.lstat().st_ino == original_inode


def test_instance_batch_recovers_complete_cohort_after_interrupted_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    alpha = make_instance(tmp_path, "alpha")
    beta = make_instance(tmp_path, "beta")
    original = store._write_instance_document
    writes = 0

    def interrupt_after_first(
        identifier: str,
        document: dict[str, object],
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected host failure")
        original(identifier, document)

    monkeypatch.setattr(store, "_write_instance_document", interrupt_after_first)
    with pytest.raises(CycloError, match="injected host failure"):
        store.save_many((alpha, beta))

    assert store.pending_batch_path.is_file()
    recovered = StateStore(store.root).list()
    assert [instance.id for instance in recovered] == ["alpha", "beta"]
    assert not store.pending_batch_path.exists()


def test_batch_recovery_never_overwrites_wrong_instance_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")

    def interrupt_publish(
        _identifier: str,
        _document: dict[str, object],
    ) -> None:
        raise OSError("injected host failure")

    monkeypatch.setattr(store, "_write_instance_document", interrupt_publish)
    with pytest.raises(CycloError, match="injected host failure"):
        store.save_many((make_instance(tmp_path, "alpha"),))
    assert store.pending_batch_path.is_file()

    directory = store.instance_dir("alpha")
    directory.mkdir()
    path = store.metadata_path("alpha")
    path.write_text(
        json.dumps(make_instance(tmp_path, "beta").as_json()) + "\n",
        encoding="utf-8",
    )
    instances, errors = StateStore(store.root).list_report()

    assert instances == []
    assert len(errors) == 1
    assert "ID mismatch" in errors[0]
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "beta"
    assert store.pending_batch_path.is_file()


def test_instance_batch_rejects_duplicate_ids_before_publication(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")

    with pytest.raises(CycloError, match="duplicate"):
        store.save_many(
            (make_instance(tmp_path, "alpha"), make_instance(tmp_path, "alpha"))
        )

    assert store.list() == []
    assert not store.pending_batch_path.exists()


def test_instance_batch_can_publish_inside_existing_state_lock(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")

    with store.locked(bind_host_config=False):
        store.save_many(
            (make_instance(tmp_path, "alpha"), make_instance(tmp_path, "beta"))
        )

    assert [instance.id for instance in store.list()] == ["alpha", "beta"]


def test_inventory_snapshot_serializes_with_batch_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    initial = StateStore(root)
    initial.save_many(
        (make_instance(tmp_path, "alpha"), make_instance(tmp_path, "beta"))
    )

    reader = StateStore(root)
    reader_paused = threading.Event()
    release_reader = threading.Event()
    reader_done = threading.Event()
    writer_attempting = threading.Event()
    writer_done = threading.Event()
    failures: list[Exception] = []
    snapshot: list[Instance] = []
    original_read_json = reader._read_json
    writer = StateStore(root)
    original_writer_lock = writer.locked

    def paused_read_json(path: Path) -> dict[str, object]:
        document = original_read_json(path)
        if path.name == "run.json" and not reader_paused.is_set():
            reader_paused.set()
            if not release_reader.wait(5):
                raise RuntimeError("test reader was not released")
        return document

    @contextmanager
    def observed_writer_lock(
        *,
        blocking: bool = True,
        bind_host_config: bool = True,
    ) -> Iterator[None]:
        writer_attempting.set()
        with original_writer_lock(
            blocking=blocking,
            bind_host_config=bind_host_config,
        ):
            yield

    monkeypatch.setattr(reader, "_read_json", paused_read_json)
    monkeypatch.setattr(writer, "locked", observed_writer_lock)

    def read_inventory() -> None:
        try:
            snapshot.extend(reader.list())
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            reader_done.set()

    def publish_batch() -> None:
        try:
            alpha = make_instance(tmp_path, "alpha")
            beta = make_instance(tmp_path, "beta")
            alpha.project_description = "updated alpha"
            beta.project_description = "updated beta"
            writer.save_many((alpha, beta))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            writer_done.set()

    reader_thread = threading.Thread(target=read_inventory, daemon=True)
    reader_thread.start()
    assert reader_paused.wait(5)

    writer_thread = threading.Thread(target=publish_batch, daemon=True)
    writer_thread.start()
    assert writer_attempting.wait(5)
    assert not writer_done.is_set()

    release_reader.set()
    reader_thread.join(5)
    writer_thread.join(5)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert reader_done.is_set()
    assert writer_done.is_set()
    assert failures == []
    assert [instance.project_description for instance in snapshot] == [
        "State test.",
        "State test.",
    ]
    assert [
        instance.project_description for instance in StateStore(root).list()
    ] == ["updated alpha", "updated beta"]


def test_instance_metadata_temporary_is_outside_uncommitted_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    instance = make_instance(tmp_path)
    observed: list[Path] = []

    def fail_replace(source: str | os.PathLike[str], _destination: object) -> None:
        observed.append(Path(source))
        raise OSError("injected host failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(CycloError, match="injected host failure"):
        store.save(instance)

    assert len(observed) == 1
    assert observed[0].parent == store.instances_dir
    assert store.list_report() == ([], [])


def test_symlinked_instance_entry_is_never_followed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    target = tmp_path / "outside"
    target.mkdir()
    store.instance_dir("linked").symlink_to(target, target_is_directory=True)

    instances, errors = store.list_report()

    assert instances == []
    assert len(errors) == 1
    assert "symlinked" in errors[0]


def test_forget_requires_stopped_intent_and_removes_all_instance_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state")
    running = make_instance(tmp_path)
    store.save(running)
    store.tasks_dir(running.id).mkdir(parents=True)
    (store.tasks_dir(running.id) / "task.md").write_text("task", encoding="utf-8")

    with pytest.raises(CycloError, match="running"):
        store.remove(running.id)

    running.intent = "stopped"
    store.save(running)
    store.remove(running.id)
    assert not store.instance_dir(running.id).exists()


def test_state_and_binding_permissions_are_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "unix:///run/user/1000/docker.sock"
    monkeypatch.setattr("cyclo.state.local_docker_endpoint", lambda: endpoint)
    store = StateStore(tmp_path / "state")

    assert store.docker_endpoint == endpoint

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.docker_endpoint_path.stat().st_mode) == 0o600


def test_docker_binding_is_stable_and_rejects_daemon_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = ["unix:///run/user/1000/docker.sock"]
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: selected[0],
    )
    root = tmp_path / "state"
    assert StateStore(root).docker_endpoint == selected[0]

    selected[0] = "unix:///var/run/docker.sock"
    with pytest.raises(CycloError, match="another Docker daemon"):
        _ = StateStore(root).docker_endpoint


def test_host_configuration_scope_is_bound_on_first_locked_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    local = StateStore(root, requested_host_config_scope="local")
    with local.locked():
        pass

    assert local.host_config_scope_path.read_text(encoding="utf-8") == "local\n"
    with pytest.raises(CycloError, match="another host configuration scope"):
        with StateStore(
            root,
            requested_host_config_scope="system",
        ).locked():
            pass


def test_interrupted_write_once_never_publishes_partial_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()

    def fail_link(*_args, **_kwargs):
        raise OSError("injected publication failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(CycloError, match="publication failure"):
        store._write_once(store.docker_endpoint_path, "unix:///socket\n", mode=0o600)

    assert not store.docker_endpoint_path.exists()
    assert list(store.root.glob(".docker-endpoint.tmp.*")) == []


@pytest.mark.parametrize(
    "identifier",
    ("", ".", "..", "/absolute", "contains space", "x" * 65),
)
def test_instance_ids_are_bounded_path_components(identifier: str) -> None:
    with pytest.raises(CycloError, match="invalid Cyclo instance ID"):
        validate_instance_id(identifier)
