from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.agentws_bundle import packaged_agentws_template
from cyclo.cli import (
    DEFAULT_GATEWAY_IMAGE,
    _DashboardUsageReader,
    cmd_repair,
    main,
    stop_instance,
)
from cyclo.errors import CycloError
from cyclo.state import Instance, StateStore


def test_run_dry_run_is_secret_free_and_does_not_create_state(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    state = tmp_path / "state"
    result = main(
        [
            "--state-root",
            str(state),
            "run",
            "--dry-run",
            str(team_repo),
            str(project_repo),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "docker run --detach" in output
    assert "gateway-token" not in output
    assert "sk-ant" not in output
    assert not state.exists()


def test_task_reuses_agentws_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = StateStore(tmp_path / "state")
    instance = Instance(
        id="alpha",
        team_name="team",
        team_path="/tmp/team",
        project_path="/tmp/project",
        generation="abc",
        providers=["openai-codex"],
        models=["openai-codex/gpt-test"],
        container_name="cyclo-alpha",
        network_name="cyclo-alpha-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
    )
    store.save(instance)
    runtime_script = Path(__file__).parents[1] / "src" / "cyclo" / "container_runtime.py"
    store.materialize_agentws("alpha", packaged_agentws_template(), runtime_script)
    spec = tmp_path / "spec.md"
    spec.write_text("# Objective\n\nTest Cyclo.\n", encoding="utf-8")

    calls: list[tuple] = []

    class FakeDocker:
        def container_running(self, name):
            calls.append(("running", name))
            return True

        def copy_to(self, container, source, destination):
            calls.append(("copy", container, source, destination))

        def exec(self, container, command, *, check=True, user=None):
            calls.append(("exec", container, tuple(command), check, user))
            return 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    result = main(
        [
            "--state-root",
            str(store.root),
            "task",
            "alpha",
            "first-task",
            str(spec),
        ]
    )

    assert result == 0
    assert any(call[0] == "copy" for call in calls)
    task_calls = [call for call in calls if call[0] == "exec" and "task-create" in call[2][0]]
    assert len(task_calls) == 1
    assert task_calls[0][2][0] == "/agentws/bin/task-create"
    assert task_calls[0][4] is None


def test_init_rejects_bad_model_before_creating_destination(tmp_path: Path) -> None:
    destination = tmp_path / "bad-team"

    result = main(
        [
            "init",
            str(destination),
            "--model",
            "not-a-proxy-model",
        ]
    )

    assert result == 1
    assert not destination.exists()


def test_init_staging_failure_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    destination = tmp_path / "partial-team"

    def fail_copytree(*_args, **_kwargs):
        raise OSError("injected staged copy failure")

    monkeypatch.setattr("cyclo.team.shutil.copytree", fail_copytree)

    result = main(
        [
            "init",
            str(destination),
            "--model",
            "openai-codex/gpt-test",
        ]
    )

    assert result == 1
    assert "injected staged copy failure" in capsys.readouterr().err
    assert not destination.exists()
    assert list(tmp_path.glob(".partial-team.new.*")) == []


def test_init_staging_failure_preserves_preexisting_empty_destination(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "empty-team"
    destination.mkdir(mode=0o711)
    destination.chmod(0o711)

    def fail_replace(_source, target):
        assert Path(target) == destination
        raise OSError("injected atomic install failure")

    monkeypatch.setattr("cyclo.team.os.replace", fail_replace)

    result = main(
        [
            "init",
            str(destination),
            "--model",
            "openai-codex/gpt-test",
            "--no-git",
        ]
    )

    assert result == 1
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert destination.stat().st_mode & 0o777 == 0o711
    assert list(tmp_path.glob(".empty-team.new.*")) == []


def test_init_existing_file_reports_clean_error(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "team-file"
    destination.write_text("keep me\n", encoding="utf-8")

    result = main(
        [
            "init",
            str(destination),
            "--model",
            "openai-codex/gpt-test",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "exists and is not a directory" in captured.err
    assert destination.read_text(encoding="utf-8") == "keep me\n"


def test_gateway_command_uses_cyclo_native_cli(monkeypatch) -> None:
    delegated: list[list[str]] = []

    def native_main(arguments: list[str]) -> int:
        delegated.append(arguments)
        return 17

    monkeypatch.setattr("cyclo.cli.gateway_cli.main", native_main)

    result = main(
        [
            "--gateway-image",
            "cyclo-gateway:test",
            "--store-volume",
            "cyclo-gateway-store-test",
            "gateway",
            "status",
            "--build",
        ]
    )

    assert result == 17
    assert delegated == [
        [
            "status",
            "--image",
            "cyclo-gateway:test",
            "--store-volume",
            "cyclo-gateway-store-test",
            "--build",
        ]
    ]


def test_gateway_destroy_store_uses_selected_volume(monkeypatch) -> None:
    delegated: list[list[str]] = []

    monkeypatch.setattr(
        "cyclo.cli.gateway_cli.main",
        lambda arguments: delegated.append(arguments) or 0,
    )

    result = main(
        [
            "--store-volume",
            "private-cyclo-store",
            "gateway",
            "destroy-store",
            "--confirm",
            "private-cyclo-store",
        ]
    )

    assert result == 0
    assert delegated == [
        [
            "destroy-store",
            "--image",
            DEFAULT_GATEWAY_IMAGE,
            "--store-volume",
            "private-cyclo-store",
            "--confirm",
            "private-cyclo-store",
        ]
    ]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["gateway", "--help"],
            "credentials, subscriptions, and retained usage history",
        ),
        (["gateway", "login", "--help"], "--api-key-stdin"),
        (["gateway", "status", "--help"], "--store-volume"),
        (["gateway", "destroy-store", "--help"], "--confirm VOLUME"),
    ],
)
def test_gateway_help_comes_from_native_gateway_parser(
    arguments: list[str], expected: str, capsys
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(arguments)

    assert stopped.value.code == 0
    output = capsys.readouterr().out
    normalized = " ".join(output.split())
    assert expected in normalized
    assert "\n  arguments\n" not in output


def test_gateway_summary_and_missing_action_cover_store_management(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "manage Cyclo's isolated gateway store" in help_text
    assert "retained usage history" in help_text

    assert main(["gateway"]) == 1
    assert "requires login, status, or destroy-store" in capsys.readouterr().err


def test_models_prints_copyable_provider_model_names(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = StateStore(tmp_path / "state")

    class FakeDocker:
        pass

    class FakeProxy:
        def catalog(self, instances, *, build=False):
            assert instances == []
            assert not build
            return {
                "openai-codex": {
                    "models": [
                        {"id": "gpt-test"},
                        {"id": "org/gpt-nested"},
                    ]
                },
                "malformed": {"models": "not-a-list"},
                "anthropic": {"models": [{"id": "claude-test"}]},
            }

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: FakeProxy())
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda _store, _docker, *, stale: [],
    )

    assert main(["models"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "anthropic/claude-test",
        "openai-codex/gpt-test",
        "openai-codex/org/gpt-nested",
    ]


def test_stop_repairs_other_networks_even_if_token_file_rotation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    store = StateStore(tmp_path / "state")
    target = Instance(
        id="alpha",
        team_name="alpha-team",
        team_path="/tmp/alpha-team",
        project_path="/tmp/alpha-project",
        generation="one",
        providers=["openai-codex"],
        models=["openai-codex/gpt-test"],
        container_name="cyclo-alpha",
        network_name="cyclo-alpha-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
        active=True,
    )
    remaining = Instance(
        id="beta",
        team_name="beta-team",
        team_path="/tmp/beta-team",
        project_path="/tmp/beta-project",
        generation="two",
        providers=["anthropic"],
        models=["anthropic/claude-test"],
        container_name="cyclo-beta",
        network_name="cyclo-beta-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
        active=True,
    )
    store.save(target)
    store.save(remaining)
    events: list[tuple] = []

    class FakeDocker:
        def container_running(self, name):
            events.append(("running", name))
            return name == "cyclo-beta"

        def ensure_network(self, name, *, offline):
            events.append(("ensure", name, offline))
            return f"{name}-id"

        def connect_gateway(self, network_id, container_id, alias):
            events.append(("connect", network_id, container_id, alias))

        def stop_remove(self, container, expected):
            events.append(("remove", container, expected))

        def remove_network(self, network, gateway_container):
            events.append(("remove-network", network, gateway_container))

    class FakeProxy:
        container_name = "gateway"
        container_id = "gateway-id"

        def reconcile(self, instances):
            events.append(("reconcile", tuple(item.id for item in instances)))

        def rotate_client_token(self, identifier):
            events.append(("rotate", identifier))
            raise CycloError("injected unlink failure")

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: FakeProxy())

    args = SimpleNamespace(gateway_image="gateway", store_volume="store")
    with pytest.raises(CycloError, match="obsolete local capability files"):
        stop_instance(args, store, "alpha")

    assert ("reconcile", ("beta",)) in events
    assert (
        "connect",
        "cyclo-beta-net-id",
        "gateway-id",
        "gateway",
    ) in events
    assert ("remove", "cyclo-alpha", "alpha") in events
    assert events.index(("rotate", "alpha")) < events.index(
        ("connect", "cyclo-beta-net-id", "gateway-id", "gateway")
    )


def test_repair_removes_container_left_by_interrupted_stop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = StateStore(tmp_path / "state")
    orphan = Instance(
        id="alpha",
        team_name="alpha-team",
        team_path="/tmp/alpha-team",
        project_path="/tmp/alpha-project",
        generation="one",
        providers=["openai-codex"],
        models=["openai-codex/gpt-test"],
        container_name="cyclo-alpha",
        network_name="cyclo-alpha-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=True,
        active=False,
    )
    store.save(orphan)
    events: list[tuple] = []

    class FakeDocker:
        def container_exists(self, name):
            events.append(("exists", name))
            return True

        def stop_remove(self, container, expected):
            events.append(("remove", container, expected))

        def remove_network(self, network, gateway_container):
            events.append(("remove-network", network, gateway_container))

    class FakeProxy:
        container_name = "gateway"

        def reconcile(self, instances, *, build=False):
            events.append(("reconcile", tuple(item.id for item in instances), build))

        def rotate_client_token(self, identifier):
            events.append(("rotate", identifier))

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: FakeProxy())
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)

    result = cmd_repair(SimpleNamespace(build_gateway=False))

    assert result == 0
    assert ("reconcile", (), False) in events
    assert ("rotate", "alpha") in events
    assert ("remove", "cyclo-alpha", "alpha") in events
    assert "cleaned 1 orphaned container" in capsys.readouterr().out


def test_dashboard_usage_reader_never_provisions_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    store = StateStore(tmp_path / "state")
    called = False

    def unexpected_gateway(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("gateway constructor must not run without an existing token")

    monkeypatch.setattr("cyclo.cli.gateway", unexpected_gateway)
    reader = _DashboardUsageReader(
        SimpleNamespace(gateway_image="gateway", store_volume="store"),
        store,
    )

    with pytest.raises(CycloError, match="not been provisioned"):
        reader.usage()

    assert called is False
    assert not store.root.exists()


def test_dashboard_command_prints_url_and_closes_server(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    events: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 43123)

        def serve_forever(self, *, poll_interval):
            assert poll_interval == 0.5
            events.append("serve")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("close")

    monkeypatch.setattr("cyclo.cli.packaged_dashboard_assets", lambda: {"/": "ok"})
    monkeypatch.setattr(
        "cyclo.cli.make_dashboard_server",
        lambda *_args, **_kwargs: FakeServer(),
    )

    result = main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "dashboard",
            "--port",
            "0",
        ]
    )

    assert result == 0
    assert events == ["serve", "close"]
    assert "http://127.0.0.1:43123/" in capsys.readouterr().out
