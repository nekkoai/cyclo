from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.agentws_bundle import packaged_agentws_template
from cyclo.cli import (
    DEFAULT_GATEWAY_IMAGE,
    _DashboardUsageReader,
    build_parser,
    cmd_provider,
    cmd_repair,
    cmd_runtime,
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
    assert "--publish 127.0.0.1::4137" in output
    assert not state.exists()


def test_run_can_publish_agentws_on_an_explicit_host(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    result = main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "run",
            "--dry-run",
            "--host",
            "0.0.0.0",
            "--port",
            "43123",
            str(team_repo),
            str(project_repo),
        ]
    )

    assert result == 0
    assert "--publish 0.0.0.0:43123:4137" in capsys.readouterr().out


def test_run_help_identifies_team_and_writable_project_mounts(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["run", "--help"])

    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "definition read-only by default" in help_text
    assert "project root writable by default" in help_text
    assert "--project-read-only" in help_text
    assert "--host" in help_text
    assert "default: 127.0.0.1" in help_text
    assert "0.0.0.0" in help_text
    assert "default: writable" in help_text
    assert "/workspace" not in help_text
    assert "/team" not in help_text


def test_run_defaults_agentws_to_loopback() -> None:
    args = build_parser().parse_args(["run", "team", "project"])

    assert args.host == "127.0.0.1"


def test_run_rejects_host_configuration_inside_writable_project(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    host_config = project_repo / "host.conf"

    result = main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "--host-config",
            str(host_config),
            "run",
            "--dry-run",
            str(team_repo),
            str(project_repo),
        ]
    )

    assert result == 1
    assert "project mount overlaps host provider configuration" in capsys.readouterr().err


def test_task_reuses_agentws_queue(
    tmp_path: Path,
    monkeypatch,
    capsys,
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
    output = capsys.readouterr().out
    assert "project root: /tmp/project" in output
    assert "task paths are relative to this project root" in output
    assert "no container mount path is required" in output


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
        (
            ["gateway", "providers", "--help"],
            "Providers are upstream AI services",
        ),
        (["gateway", "status", "--help"], "--store-volume"),
        (
            ["gateway", "restart", "--help"],
            "Recreate Cyclo's credential gateway",
        ),
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
    assert "discover providers" in help_text
    assert "isolated gateway" in help_text
    assert "retained usage history" in help_text

    assert main(["gateway"]) == 1
    assert (
        "requires providers, login, status, restart, or destroy-store"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["runtime", "--help"], "start the current runtime image"),
        (["runtime", "restart", "--help"], "explicitly replace the runtime container"),
        (["provider", "--help"], "wait for readiness"),
        (["provider", "build", "--help"], "without launching them"),
        (["models", "--help"], "Refresh the running provider runtime"),
    ],
)
def test_runtime_provider_and_models_help_describes_explicit_boundaries(
    arguments: list[str], expected: str, capsys
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(arguments)

    assert stopped.value.code == 0
    assert expected in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("network_failure", [False, True])
def test_stop_repairs_network_before_publishing_team_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_failure: bool,
) -> None:
    store = StateStore(tmp_path / "state")
    target = Instance(
        id="target",
        team_name="target-team",
        team_path="/team/target",
        project_path="/project/target",
        generation="target-generation",
        providers=["account"],
        models=["account/model"],
        container_name="cyclo-target",
        network_name="cyclo-target-net",
        image="cyclo-runtime:test",
        team_write=False,
        project_read_only=False,
        offline=False,
        active=True,
    )
    store.save(target)
    remaining = SimpleNamespace(id="remaining")
    events: list[object] = []

    class FakeRuntime:
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def update_clients(instances):
            events.append(("publish", tuple(item.id for item in instances)))

        @staticmethod
        def rotate_client_token(identifier):
            events.append(("rotate", identifier))

    class FakeDocker:
        @staticmethod
        def stop_remove(container, identifier):
            events.append(("stop", container, identifier))

        @staticmethod
        def remove_network(network, runtime):
            events.append(("remove-network", network, runtime))

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda _store, _docker, *, stale: [remaining],
    )
    def attach(_docker, _runtime, instances):
        events.append(("attach", tuple(item.id for item in instances)))
        if network_failure:
            raise CycloError("injected network drift")

    monkeypatch.setattr("cyclo.cli.attach_active_networks", attach)

    if network_failure:
        with pytest.raises(CycloError, match="network repair failed"):
            stop_instance(SimpleNamespace(), store, "target")
    else:
        stop_instance(SimpleNamespace(), store, "target")

    assert events == [
        ("attach", ("remaining",)),
        ("publish", ("remaining",)),
        ("rotate", "target"),
        ("stop", "cyclo-target", "target"),
        ("remove-network", "cyclo-target-net", "cyclo-provider-runtime-test"),
    ]
    assert store.load("target").active is False


def test_runtime_start_seeds_clients_without_reloading_old_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    events: list[object] = []

    class FakeRuntime:
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def status():
            return SimpleNamespace(exists=False, running=False, current=False)

        @staticmethod
        def update_clients(instances, *, apply_runtime):
            events.append(("seed", tuple(instances), apply_runtime))

        @staticmethod
        def start(*, build):
            events.append(("start", build))

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.active_instances", lambda _store, _docker: [])
    monkeypatch.setattr(
        "cyclo.cli.attach_active_networks",
        lambda _docker, _runtime, instances: events.append(
            ("attach", tuple(instances))
        ),
    )

    assert cmd_runtime(SimpleNamespace(runtime_action="start", build=False)) == 0

    assert events == [("seed", (), False), ("start", False), ("attach", ())]
    assert "started provider runtime" in capsys.readouterr().out


def test_runtime_start_applies_clients_when_current_process_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    events: list[object] = []

    class FakeRuntime:
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def status():
            return SimpleNamespace(exists=True, running=True, current=True)

        @staticmethod
        def update_clients(instances):
            events.append(("apply", tuple(instances)))

        @staticmethod
        def start(*, build):
            events.append(("start", build))

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.active_instances", lambda _store, _docker: [])
    monkeypatch.setattr("cyclo.cli.attach_active_networks", lambda *_args: None)

    assert cmd_runtime(SimpleNamespace(runtime_action="start", build=False)) == 0

    assert events == [("apply", ()), ("start", False)]


def test_runtime_start_rejects_stale_process_before_registry_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")

    class FakeRuntime:
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def status():
            return SimpleNamespace(exists=True, running=True, current=False)

        @staticmethod
        def update_clients(*_args, **_kwargs):
            raise AssertionError("stale runtime registries must not be mutated")

        @staticmethod
        def start(*, build):
            assert build is False
            raise CycloError("run `cyclo runtime restart`")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.active_instances", lambda _store, _docker: [])

    with pytest.raises(CycloError, match="runtime restart"):
        cmd_runtime(SimpleNamespace(runtime_action="start", build=False))


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (False, ["stop", "seed", ("start", False)]),
        (True, ["build", "stop", "seed", ("start", False)]),
    ],
)
def test_runtime_restart_removes_old_authority_before_seeding_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build: bool,
    expected: list[object],
) -> None:
    store = StateStore(tmp_path / "state")
    events: list[object] = []

    class FakeRuntime:
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def build():
            events.append("build")

        @staticmethod
        def stop():
            events.append("stop")

        @staticmethod
        def update_clients(instances, *, apply_runtime):
            assert tuple(instances) == ()
            assert apply_runtime is False
            events.append("seed")

        @staticmethod
        def start(*, build):
            events.append(("start", build))

        @staticmethod
        def restart(*, build):
            raise AssertionError(f"unsafe in-place restart called: {build}")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.active_instances", lambda _store, _docker: [])
    monkeypatch.setattr("cyclo.cli.attach_active_networks", lambda *_args: None)

    assert cmd_runtime(SimpleNamespace(runtime_action="restart", build=build)) == 0

    assert events == expected


@pytest.mark.parametrize(
    ("launched", "expected_marker", "verb"),
    [(True, 1234.567, "started"), (False, None, "running")],
)
def test_provider_start_requires_matching_registration_and_freshness_for_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    launched: bool,
    expected_marker: float | None,
    verb: str,
) -> None:
    store = StateStore(tmp_path / "state")
    definition = SimpleNamespace(prefix="local")
    identity = SimpleNamespace(prefix="local")
    item = SimpleNamespace(
        definition=definition,
        identity=identity,
        generation="expected-generation",
    )
    wait_call: dict[str, object] = {}

    class FakeComponentRuntime:
        def require_startable(self, _spec):
            return None

        def start(self, _spec):
            return SimpleNamespace(
                container_restarted=launched,
                generation="expected-generation",
            )

    component_runtime = FakeComponentRuntime()

    class FakeHost:
        def __init__(self, _state_root):
            self.runtime = component_runtime

        def prepare(self, definitions, *, selected_prefixes):
            assert tuple(definitions) == (definition,)
            assert selected_prefixes == {"local"}
            return (item,)

        def spec(self, selected):
            assert selected is item
            return SimpleNamespace()

        def published_expectations(self):
            return []

        def upsert_expectations(self, _expectations):
            return None

        def expectation(self, selected):
            assert selected is item
            return {"prefix": "local"}

        def client_record(self, selected):
            assert selected is item
            return {"client_id": "provider-local"}

    class FakeService:
        def require_running(self):
            return 8788

        def provider_clients(self):
            return ()

        def merged_provider_clients(self, records):
            return tuple(records)

        def update_clients(self, instances, *, provider_clients):
            assert instances == []
            assert provider_clients == ({"client_id": "provider-local"},)
            return {}

        def wait_provider(self, prefix, generation, **kwargs):
            wait_call.update(
                prefix=prefix,
                generation=generation,
                **kwargs,
            )

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.HostProviders", FakeHost)
    monkeypatch.setattr("cyclo.cli.provider_service", lambda _args, _store: FakeService())
    monkeypatch.setattr(
        "cyclo.cli.host_configuration",
        lambda _args: SimpleNamespace(load=lambda: (definition,)),
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.active_instances", lambda _store, _docker: [])
    monkeypatch.setattr("cyclo.cli.time.time_ns", lambda: 1_234_567_890_000)
    args = SimpleNamespace(
        provider_action="start",
        all_providers=False,
        provider_prefix="local",
        build=False,
    )

    assert cmd_provider(args) == 0
    assert wait_call == {
        "prefix": "local",
        "generation": "expected-generation",
        "runtime": component_runtime,
        "identity": identity,
        "registered_after": expected_marker,
    }
    assert capsys.readouterr().out == f"{verb} provider: local\n"


def test_provider_restart_stops_old_process_before_publishing_new_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    definition = SimpleNamespace(prefix="local")
    identity = SimpleNamespace(prefix="local")
    item = SimpleNamespace(
        definition=definition,
        identity=identity,
        generation="new-generation",
    )
    events: list[object] = []

    class FakeComponentRuntime:
        @staticmethod
        def require_current_image(_spec):
            events.append("image-current")

        @staticmethod
        def stop(selected):
            assert selected is identity
            events.append("stop-old")
            return True

        @staticmethod
        def start(_spec):
            events.append("start-new")
            return SimpleNamespace(
                container_restarted=True,
                generation="new-generation",
            )

    component_runtime = FakeComponentRuntime()

    class FakeHost:
        def __init__(self, _state_root):
            self.runtime = component_runtime

        @staticmethod
        def prepare(definitions, *, selected_prefixes):
            assert tuple(definitions) == (definition,)
            assert selected_prefixes == {"local"}
            return (item,)

        @staticmethod
        def spec(_item):
            return SimpleNamespace()

        @staticmethod
        def published_expectations():
            return []

        @staticmethod
        def remove_expectations(prefixes):
            assert tuple(prefixes) == ("local",)
            events.append("revoke-expectation")

        @staticmethod
        def rotate_capabilities(selected):
            assert selected is item
            events.append("rotate-capabilities")

        @staticmethod
        def expectation(_item):
            return {"prefix": "local", "generation": "new-generation"}

        @staticmethod
        def upsert_expectations(_records):
            events.append("publish-expectation")

        @staticmethod
        def client_record(_item):
            return {"client_id": "provider-local"}

    class FakeService:
        @staticmethod
        def capability_update_guard():
            return nullcontext()

        @staticmethod
        def reload_control(*, require_current):
            assert require_current is False
            events.append("reload-expectation")

        @staticmethod
        def require_running():
            return 8788

        @staticmethod
        def provider_clients():
            return (
                {"client_id": "provider-local-old", "provider_prefix": "local"},
                {"client_id": "provider-other", "provider_prefix": "other"},
            )

        @staticmethod
        def merged_provider_clients(records):
            return tuple(records)

        @staticmethod
        def update_clients(_instances, *, provider_clients):
            if provider_clients == (
                {"client_id": "provider-other", "provider_prefix": "other"},
            ):
                events.append("publish-revocation")
            else:
                assert provider_clients == ({"client_id": "provider-local"},)
                events.append("publish-client")
            return {}

        @staticmethod
        def wait_provider(*_args, **_kwargs):
            events.append("ready")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.HostProviders", FakeHost)
    monkeypatch.setattr("cyclo.cli.provider_service", lambda _args, _store: FakeService())
    monkeypatch.setattr(
        "cyclo.cli.host_configuration",
        lambda _args: SimpleNamespace(load=lambda: (definition,)),
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.active_instances", lambda _store, _docker: [])

    assert cmd_provider(
        SimpleNamespace(
            provider_action="restart",
            all_providers=False,
            provider_prefix="local",
            build=False,
        )
    ) == 0

    assert events == [
        "image-current",
        "revoke-expectation",
        "reload-expectation",
        "publish-revocation",
        "stop-old",
        "rotate-capabilities",
        "publish-expectation",
        "publish-client",
        "start-new",
        "ready",
    ]


def test_provider_status_uses_definitions_and_lists_absent_and_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    stale_source = tmp_path / "stale-source"
    absent_source = tmp_path / "absent-source"
    for source in (stale_source, absent_source):
        source.mkdir()
        (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    host_config = tmp_path / "host.conf"
    host_config.write_text(
        f"provider stale {stale_source} account/model mode=new\n"
        f"provider absent {absent_source} account/model\n",
        encoding="utf-8",
    )
    seen_specs: dict[str, object] = {}

    class FakeProviderRuntime:
        def __init__(self, _state_root):
            pass

        def identity(self, prefix):
            return SimpleNamespace(prefix=prefix)

        def owned_identities(self):
            return (self.identity("orphan"), self.identity("stale"))

        def status(self, identity, spec=None):
            seen_specs[identity.prefix] = spec
            if identity.prefix == "absent":
                return SimpleNamespace(
                    image_exists=False,
                    image_current=False,
                    container_exists=False,
                    container_running=False,
                    configuration_current=False,
                )
            if identity.prefix == "stale":
                return SimpleNamespace(
                    image_exists=True,
                    image_current=True,
                    container_exists=True,
                    container_running=True,
                    configuration_current=False,
                )
            return SimpleNamespace(
                image_exists=True,
                image_current=False,
                container_exists=True,
                container_running=True,
                configuration_current=False,
            )

    monkeypatch.setattr("cyclo.cli.ProviderRuntime", FakeProviderRuntime)

    assert main(
        [
            "--state-root",
            str(state),
            "--host-config",
            str(host_config),
            "provider",
            "status",
            "--all",
        ]
    ) == 0

    assert capsys.readouterr().out.splitlines() == [
        "absent\tabsent",
        "orphan\trunning\tunconfigured",
        "stale\trunning\tstale",
    ]
    assert seen_specs["absent"].arguments == ("account/model",)
    assert seen_specs["stale"].arguments == ("account/model", "mode=new")
    assert seen_specs["orphan"] is None

    seen_specs.clear()
    assert main(
        [
            "--state-root",
            str(state),
            "--host-config",
            str(host_config),
            "provider",
            "status",
            "stale",
        ]
    ) == 0
    assert capsys.readouterr().out == "stale\trunning\tstale\n"
    assert seen_specs["stale"].arguments == ("account/model", "mode=new")


def test_doctor_reports_configured_provider_staleness_and_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    stale_source = tmp_path / "stale-source"
    absent_source = tmp_path / "absent-source"
    for source in (stale_source, absent_source):
        source.mkdir()
        (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    host_config = tmp_path / "host.conf"
    host_config.write_text(
        f"provider stale {stale_source} account/model mode=new\n"
        f"provider absent {absent_source} account/model\n",
        encoding="utf-8",
    )
    runtime_state = tmp_path / "state" / "provider-runtime"

    class FakeDocker:
        def available(self):
            return True, "test daemon"

    class FakeService:
        state_root = runtime_state
        container_name = "cyclo-provider-runtime-test"

        def status(self):
            return SimpleNamespace(running=True, current=True)

        def catalog(self):
            return {"stale": {}, "absent": {}}

    class FakeComponentRuntime:
        def __init__(self, state_root):
            assert state_root == runtime_state

        def status(self, identity, spec):
            assert spec.identity == identity
            if identity.prefix == "absent":
                return SimpleNamespace(
                    image_current=False,
                    configuration_current=False,
                    container_exists=False,
                    container_running=False,
                )
            return SimpleNamespace(
                image_current=True,
                configuration_current=False,
                container_exists=True,
                container_running=True,
            )

    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: tmp_path / "agentws")
    monkeypatch.setattr(
        "cyclo.cli.gateway",
        lambda _args, _store: SimpleNamespace(
            gateway=SimpleNamespace(__file__="credential-gateway.py")
        ),
    )
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeService()
    )
    monkeypatch.setattr("cyclo.cli.ProviderRuntime", FakeComponentRuntime)

    assert main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "--host-config",
            str(host_config),
            "doctor",
        ]
    ) == 1

    output = capsys.readouterr().out
    assert "no  configured provider stale: stale (configuration)" in output
    assert "no  configured provider absent: absent" in output


def test_gateway_restart_is_credential_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = StateStore(tmp_path / "state")
    events: list[tuple] = []

    class FakeGateway:
        def restart(self, *, build=False):
            events.append(("restart", build))

    class FakeRuntime:
        @staticmethod
        def status():
            events.append(("runtime-status",))
            return SimpleNamespace(running=True)

        @staticmethod
        def refresh_catalog_control():
            events.append(("runtime-reload",))

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: FakeGateway())
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr(
        "cyclo.cli.Docker",
        lambda: (_ for _ in ()).throw(
            AssertionError("gateway restart must not touch team networks")
        ),
    )

    assert main(["gateway", "restart", "--build"]) == 0
    assert events == [
        ("restart", True),
        ("runtime-status",),
        ("runtime-reload",),
    ]
    assert capsys.readouterr().out == "restarted gateway\n"


@pytest.mark.parametrize("running", [False, True])
def test_successful_gateway_login_refreshes_runtime_only_when_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
) -> None:
    store = StateStore(tmp_path / "state")
    events: list[object] = []

    class FakeRuntime:
        @staticmethod
        def status():
            events.append("status")
            return SimpleNamespace(running=running)

        @staticmethod
        def refresh_catalog_control():
            events.append("reload")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr(
        "cyclo.cli.gateway_cli.main",
        lambda arguments: events.append(tuple(arguments)) or 0,
    )

    assert main(["gateway", "login", "openai", "--api-key-stdin"]) == 0

    assert isinstance(events[0], tuple)
    assert events[1:] == (["status", "reload"] if running else ["status"])


def test_models_is_a_pure_provider_runtime_query(tmp_path: Path, monkeypatch, capsys) -> None:
    store = StateStore(tmp_path / "state")

    class FakeRuntime:
        @staticmethod
        def catalog(*, refresh):
            assert refresh is True
            return {
                "openai-codex": {"models": [{"id": "gpt-test"}]},
                "anthropic": {"models": [{"id": "claude-test"}]},
            }

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )
    monkeypatch.setattr(
        "cyclo.cli.Docker",
        lambda: (_ for _ in ()).throw(AssertionError("models must not use Docker")),
    )

    assert main(["models"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "anthropic/claude-test",
        "openai-codex/gpt-test",
    ]


def test_provider_stop_revokes_capabilities_before_failed_container_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    events: list[tuple[str, tuple[str, ...] | str]] = []

    class FakeRuntime:
        @staticmethod
        def identity(prefix):
            return SimpleNamespace(prefix=prefix)

        @staticmethod
        def stop(identity):
            events.append(("stop", identity.prefix))
            raise CycloError("injected Docker stop failure")

    class FakeHost:
        def __init__(self, _root):
            self.runtime = FakeRuntime()

        @staticmethod
        def remove_expectations(prefixes):
            events.append(("revoke-expectations", tuple(prefixes)))

    class FakeService:
        @staticmethod
        def capability_update_guard():
            return nullcontext()

        @staticmethod
        def reload_control(*, require_current=True):
            assert require_current is False
            events.append(("apply-expectations", "runtime"))

        @staticmethod
        def remove_provider_clients(prefixes):
            events.append(("revoke-upstream", tuple(prefixes)))
            raise CycloError("injected registry write failure")

    monkeypatch.setattr("cyclo.cli.HostProviders", FakeHost)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeService()
    )

    assert main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "provider",
            "stop",
            "fusion",
        ]
    ) == 1
    assert events == [
        ("revoke-expectations", ("fusion",)),
        ("apply-expectations", "runtime"),
        ("revoke-upstream", ("fusion",)),
        ("stop", "fusion"),
    ]
    error = capsys.readouterr().err
    assert "injected registry write failure" in error
    assert "injected Docker stop failure" in error


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
    server_args: dict[str, object] = {}

    class FakeServer:
        server_address = ("0.0.0.0", 43123)

        def serve_forever(self, *, poll_interval):
            assert poll_interval == 0.5
            events.append("serve")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("close")

    monkeypatch.setattr("cyclo.cli.packaged_dashboard_assets", lambda: {"/": "ok"})
    monkeypatch.setattr(
        "cyclo.cli.make_dashboard_server",
        lambda *_args, **kwargs: server_args.update(kwargs) or FakeServer(),
    )

    result = main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "dashboard",
            "--host",
            "0.0.0.0",
            "--port",
            "0",
        ]
    )

    assert result == 0
    assert events == ["serve", "close"]
    assert server_args["host"] == "0.0.0.0"
    output = capsys.readouterr().out
    assert "http://0.0.0.0:43123/" in output
    assert "WARNING" in output
    assert "no authentication" in output


def test_dashboard_help_warns_about_non_loopback_exposure(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["dashboard", "--help"])

    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--host" in help_text
    assert "default: 127.0.0.1" in help_text
    assert "0.0.0.0" in help_text
    assert "no authentication" in help_text


def test_dashboard_defaults_to_loopback() -> None:
    args = build_parser().parse_args(["dashboard"])

    assert args.host == "127.0.0.1"
