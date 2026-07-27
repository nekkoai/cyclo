from __future__ import annotations

import argparse
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.agentws_bundle import packaged_agentws_root
from cyclo.agentws_queue import AgentSupervisorStatus
from cyclo.cli import (
    DEFAULT_HOST_CONFIG,
    _prepare_team_images,
    build_parser,
    host_config,
    main,
    state_store,
)
from cyclo.component import COMPONENT_INTERFACE, PROVIDER_INTERFACE
from cyclo.docker import Docker, DockerContainerState
from cyclo.errors import CycloError
from cyclo.health import ProviderHealth
from cyclo.installation import (
    derived_team_image_name,
    installation_id,
    team_container_name,
    team_image_name,
    team_network_name,
)
from cyclo.project import MAX_PROJECT_FILE_BYTES, load_project
from cyclo.state import Instance, StateStore
from cyclo.team import load_team


MODELS = ("openai-codex/gpt-test", "anthropic/claude-test")


def catalogue_model(identifier: str) -> dict[str, object]:
    return {
        "id": identifier,
        "displayName": identifier,
        "capabilities": {
            "inputModalities": ["MODALITY_TEXT"],
            "outputModalities": ["MODALITY_TEXT"],
        },
        "contextWindowTokens": "128000",
        "maxOutputTokens": "4096",
        "inferenceFormat": "pi-ai@0.81.1",
    }


def write_project(
    path: Path,
    team: Path,
    project: Path,
    *,
    second_team: Path | None = None,
) -> Path:
    documentation = path.parent / "documentation"
    documentation.mkdir(exist_ok=True)
    lines = [
        "name integration-project",
        "description Exercise configured teams and named mounts.",
        f"team {team} ro",
    ]
    if second_team is not None:
        lines.append(f"team {second_team} rw")
    lines.extend(
        (
            f"mount source {project} rw",
            f"mount documentation {documentation} ro",
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def instance(
    identifier: str,
    root: Path,
    *,
    active: bool = False,
    project_file: Path | None = None,
) -> Instance:
    configured = project_file is not None
    return Instance(
        id=identifier,
        team_name=f"team-{identifier}",
        team_path=str((root / f"team-{identifier}").resolve()),
        project_path=str(root.resolve()),
        generation="team-generation",
        providers=["gateway"],
        models=[MODELS[0]],
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image="cyclo-team:test",
        team_write=False,
        offline=False,
        launch_id="0" * 32,
        active=active,
        project_name="integration-project" if configured else "",
        project_file=str(project_file.resolve()) if configured else "",
        project_description="Configured project" if configured else "",
        project_generation="project-generation" if configured else "",
        project_mounts=(
            [{"name": "source", "path": str(root.resolve()), "mode": "rw"}]
            if configured
            else []
        ),
    )


def persist(store: StateStore, selected: Instance) -> None:
    selected.container_name = team_container_name(store.system, selected.id)
    selected.network_name = team_network_name(store.system, selected.id)
    store.save(selected)


def component_status(
    *,
    name: str = "gateway",
    present: bool = True,
    running: bool = True,
    current: bool = True,
    container_state: str = "running",
    ready: bool = True,
    error: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        kind="gateway" if name == "gateway" else "passthrough",
        image_id="sha256:" + "a" * 64,
        container_id="b" * 64 if present else None,
        running=running,
        current=current,
        container_state=container_state,
        engine_health="healthy" if running else "none",
        health="ready" if ready else "not-ready",
        error=error,
        works=bool(present and running and current and ready and not error),
    )


def gateway_status(*, ready: bool = True, current: bool = True) -> SimpleNamespace:
    return component_status(
        name="gateway",
        ready=ready,
        current=current,
    )


def provider_connection(
    socket_path: Path,
    *,
    ready: bool = True,
) -> SimpleNamespace:
    component = component_status(
        name="pass",
        ready=ready,
    )
    return SimpleNamespace(
        generation="provider-generation",
        socket_path=socket_path,
        components=(gateway_status(), component),
    )


class RecordingStore:
    def __init__(self, root: Path, instances: list[object] | None = None) -> None:
        self.root = root
        self.components_root = root / "components"
        self.instances = instances or []
        self.lock_entries = 0

    @property
    def system(self) -> str:
        return StateStore(self.root).system

    @contextmanager
    def locked(self):
        self.lock_entries += 1
        yield

    def list(self):
        return list(self.instances)

    def queue_root(self, identifier: str) -> Path:
        return self.root / "instances" / identifier / "agentws-state"


class RunSystem:
    def __init__(self, socket_path: Path) -> None:
        self.connection_value = provider_connection(socket_path)
        self.configured_socket_path = socket_path
        self.configuration = SimpleNamespace(generation="provider-generation")
        self.gateway = SimpleNamespace(
            socket_path=Path("/run/cyclo/gateway/component.sock")
        )
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")
        return self.connection_value

    def catalogue(self, connection):
        self.calls.append("catalogue")
        assert connection is self.connection_value
        return (
            connection,
            {"models": [catalogue_model(model) for model in MODELS]},
        )


def install_run_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> RunSystem:
    source = tmp_path / "agentws"
    source.mkdir()
    socket_dir = tmp_path / "provider-socket"
    socket_dir.mkdir()
    stack = RunSystem(socket_dir / "component.sock")
    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: source)
    monkeypatch.setattr("cyclo.cli.provider_system", lambda *_args, **_kwargs: stack)
    monkeypatch.setattr(
        "cyclo.cli._prepare_team_images",
        lambda _bindings, **_kwargs: None,
    )
    return stack


def subcommands(parser: argparse.ArgumentParser) -> set[str]:
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_parser_exposes_only_the_cutover_command_surface() -> None:
    parser = build_parser()

    assert subcommands(parser) == {
        "team",
        "project",
        "validate",
        "refresh",
        "run",
        "stop",
        "forget",
        "ps",
        "inspect",
        "dashboard",
        "task",
        "logs",
        "path",
        "usage",
        "models",
        "repair",
        "component",
        "providers",
        "gateway",
        "doctor",
    }
    assert "runtime" not in subcommands(parser)
    assert "provider" not in subcommands(parser)


@pytest.mark.parametrize(
    "argv",
    (
        ["init", "TEAM", "--model", "provider/model"],
        ["templates"],
        ["run", "TEAM", "PROJECT"],
        ["run", "--team-write", "project.cyclo"],
        ["run", "--project-read-only", "project.cyclo"],
        ["run", "--name", "legacy", "project.cyclo"],
        ["runtime", "status"],
        ["provider", "status"],
    ),
)
def test_parser_rejects_removed_entry_points(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(argv)

    assert stopped.value.code == 2


def test_run_parser_has_one_project_argument_and_loopback_default() -> None:
    args = build_parser().parse_args(["run", "project.cyclo"])

    assert args.project == "project.cyclo"
    assert not hasattr(args, "team")
    assert args.host == "127.0.0.1"


def test_state_root_selects_the_host_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CYCLO_STATE_ROOT", raising=False)
    default_args = build_parser().parse_args(["doctor"])
    default_store = state_store(default_args)
    assert host_config(default_args, default_store) == DEFAULT_HOST_CONFIG

    selected = tmp_path / "installation"
    explicit_args = build_parser().parse_args(
        ["--state-root", str(selected), "doctor"]
    )
    explicit_store = state_store(explicit_args)
    assert host_config(explicit_args, explicit_store) == selected / "host.conf"
    assert not hasattr(explicit_args, "host_config")

    configured = tmp_path / "environment-installation"
    monkeypatch.setenv("CYCLO_STATE_ROOT", str(configured))
    environment_args = build_parser().parse_args(["doctor"])
    environment_store = state_store(environment_args)
    assert host_config(environment_args, environment_store) == configured / "host.conf"


def test_team_commands_own_initialization_and_template_discovery() -> None:
    parser = build_parser()

    init_args = parser.parse_args(
        ["team", "init", "./team", "--model", "provider/model"]
    )
    templates_args = parser.parse_args(["team", "templates"])

    assert init_args.team == "./team"
    assert init_args.model == "provider/model"
    assert init_args.func.__name__ == "cmd_team_init"
    assert templates_args.func.__name__ == "cmd_team_templates"


def test_project_init_writes_a_valid_definition_without_overwriting(
    tmp_path: Path,
    team_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    definition = tmp_path / "demo" / "project.cyclo"
    context = tmp_path / "project-context.md"
    context.write_text(
        "`source` contains the RTL implementation.\n"
        "CYCLO_CONTEXT\n",
        encoding="utf-8",
    )
    arguments = [
        "project",
        "init",
        str(definition),
        "--description",
        "RTL integration project",
        "--context",
        str(context),
        "--team",
        str(team_repo),
        "ro",
        "--mount",
        "source",
        str(source),
        "rw",
    ]

    assert main(arguments) == 0
    project = load_project(definition)
    assert project.name == "demo"
    assert project.description == "RTL integration project"
    assert project.context == (
        "`source` contains the RTL implementation.\nCYCLO_CONTEXT"
    )
    assert project.teams[0].path == team_repo.resolve()
    assert project.mounts[0].path == source.resolve()
    assert "next: cyclo validate" in capsys.readouterr().out
    assert "context <<CYCLO_CONTEXT_2" in definition.read_text(encoding="utf-8")

    assert main(arguments) == 1
    assert "refusing to overwrite" in capsys.readouterr().err


def test_project_init_rejects_bad_input_without_leaving_a_definition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    team = tmp_path / "team"
    source = tmp_path / "source"
    team.mkdir()
    source.mkdir()
    definition = tmp_path / "project.cyclo"

    assert main(
        [
            "project",
            "init",
            str(definition),
            "--team",
            str(team),
            "execute",
            "--mount",
            "source",
            str(source),
            "rw",
        ]
    ) == 1
    assert "invalid team access mode" in capsys.readouterr().err
    assert not definition.exists()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "must not be empty"),
        (b"\xff", "not valid UTF-8"),
        (b"x" * (MAX_PROJECT_FILE_BYTES + 1), "exceeds"),
    ],
    ids=("empty", "invalid-utf8", "oversized"),
)
def test_project_init_rejects_invalid_context_without_creating_definition(
    tmp_path: Path,
    team_repo: Path,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
    message: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    context = tmp_path / "context.md"
    context.write_bytes(content)
    definition = tmp_path / "project.cyclo"

    assert main(
        [
            "project",
            "init",
            str(definition),
            "--context",
            str(context),
            "--team",
            str(team_repo),
            "ro",
            "--mount",
            "source",
            str(source),
            "rw",
        ]
    ) == 1

    assert message in capsys.readouterr().err
    assert not definition.exists()


def test_refresh_stops_then_refreshes_provider_system_and_active_projects(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (team_repo / "Dockerfile").write_text(
        "ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n",
        encoding="utf-8",
    )
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
    )
    selected = instance(
        "integration-project-review-team",
        tmp_path,
        active=True,
        project_file=definition,
    )
    selected.agentws_host = "0.0.0.0"
    selected.offline = True
    selected.port = 4317
    store = RecordingStore(tmp_path / "state", [selected])
    events: list[str] = []

    class RefreshStack:
        @staticmethod
        def refresh():
            events.append("provider-system-refresh")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: object())
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda selected_store, _docker: list(selected_store.instances),
    )
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda _args, _store: RefreshStack(),
    )
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda _args, _store, instance: events.append(f"stop:{instance.id}"),
    )

    def run(project_args):
        assert project_args.project == str(definition)
        assert project_args.image is None
        assert project_args.offline is True
        assert project_args.host == "0.0.0.0"
        assert project_args.port == 4317
        events.append(f"run:{project_args.project}")
        return 0

    monkeypatch.setattr("cyclo.cli.cmd_run", run)

    assert main(["refresh"]) == 0
    stopped = "stop:integration-project-review-team"
    assert events.index(stopped) < events.index("provider-system-refresh")
    assert events.index("provider-system-refresh") < events.index(f"run:{definition}")
    assert "Cyclo refresh complete" in capsys.readouterr().out


def test_refresh_refuses_active_legacy_instance_before_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = instance("legacy", tmp_path, active=True)
    store = RecordingStore(tmp_path / "state", [selected])
    built: list[bool] = []
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: object())
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda selected_store, _docker: list(selected_store.instances),
    )
    monkeypatch.setattr(
        "cyclo.cli.gateway",
        lambda _args, _store: built.append(True),
    )

    assert main(["refresh"]) == 1
    assert built == []
    assert "cannot refresh legacy instances without project.cyclo: legacy" in capsys.readouterr().err


def test_refresh_refuses_partially_active_project_before_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_team = tmp_path / "first"
    second_team = tmp_path / "second"
    project = tmp_path / "source"
    for path in (first_team, second_team, project):
        path.mkdir()
    definition = write_project(
        tmp_path / "project.cyclo",
        first_team,
        project,
        second_team=second_team,
    )
    selected = instance(
        "integration-project-first", tmp_path, active=True, project_file=definition
    )
    store = RecordingStore(tmp_path / "state", [selected])
    built: list[bool] = []
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: object())
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda selected_store, _docker: list(selected_store.instances),
    )
    monkeypatch.setattr(
        "cyclo.cli.gateway",
        lambda _args, _store: built.append(True),
    )

    assert main(["refresh"]) == 1
    assert built == []
    assert "cannot refresh partially active project" in capsys.readouterr().err


def test_project_dry_run_expands_team_and_mount_authority_without_state(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    state_root = tmp_path / "state"
    stack = install_run_fakes(monkeypatch, tmp_path)

    result = main(
        ["--state-root", str(state_root), "run", "--dry-run", str(definition)]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert stack.calls == []
    assert output.count("docker run --detach") == 2
    assert output.count(f"src={project_repo.resolve()},dst=/workspace/source") == 2
    assert output.count("dst=/readonly/documentation,readonly") == 2
    assert f"src={team_repo.resolve()},dst=/team,readonly" in output
    assert f"src={second_team.resolve()},dst=/team" in output
    assert f"src={second_team.resolve()},dst=/team,readonly" not in output
    assert "CYCLO_PROJECT_MANIFEST=" not in output
    assert "gateway-token" not in output
    assert "Authorization" not in output
    system = installation_id(state_root / "components")
    assert (
        f"--name {team_container_name(system, 'integration-project-review-team')}"
        in output
    )
    assert team_image_name(system, "0.2.0") in output
    assert not state_root.exists()


def test_project_dry_run_selects_a_derived_image_per_team_dockerfile(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    derived_team = tmp_path / "rtl-team"
    shutil.copytree(team_repo, derived_team)
    (derived_team / "Dockerfile").write_text(
        "ARG CYCLO_TEAM_BASE\n"
        "FROM ${CYCLO_TEAM_BASE}\n"
        "RUN apt-get update && apt-get install -y verilator\n",
        encoding="utf-8",
    )
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=derived_team,
    )
    state_root = tmp_path / "state"
    install_run_fakes(monkeypatch, tmp_path)

    assert (
        main(
            [
                "--state-root",
                str(state_root),
                "run",
                "--dry-run",
                str(definition),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    system = installation_id(state_root / "components")
    assert team_image_name(system, "0.2.0") in output
    assert (
        derived_team_image_name(system, "0.2.0", derived_team, "rtl-team")
        in output
    )


def test_run_has_no_manual_build_mode(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
    )
    install_run_fakes(monkeypatch, tmp_path)

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "run",
                "--dry-run",
                "--build",
                str(definition),
            ]
        )
    assert "unrecognized arguments: --build" in capsys.readouterr().err


def test_prepare_team_images_resolves_base_and_derived_tags_to_exact_ids(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = load_team(team_repo)
    derived_root = tmp_path / "derived"
    shutil.copytree(team_repo, derived_root)
    (derived_root / "Dockerfile").write_text(
        "ARG CYCLO_TEAM_BASE\nFROM ${CYCLO_TEAM_BASE}\n",
        encoding="utf-8",
    )
    derived = load_team(derived_root)
    base_id = "sha256:" + "a" * 64
    derived_id = "sha256:" + "b" * 64
    plain_instance = SimpleNamespace(image="base:tag", image_override="")
    derived_instance = SimpleNamespace(image="derived:tag", image_override="")
    bindings = (
        SimpleNamespace(team=plain, instance=plain_instance),
        SimpleNamespace(team=derived, instance=derived_instance),
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "cyclo.cli.ensure_team_runtime_image",
        lambda image: calls.append(("base", image)) or base_id,
    )
    monkeypatch.setattr(
        "cyclo.cli.ensure_derived_team_image",
        lambda image, root, base: calls.append(
            ("derived", image, root, base)
        )
        or derived_id,
    )

    assert (
        _prepare_team_images(
            bindings,
            base_image="base:tag",
        )
        == base_id
    )
    assert plain_instance.image == base_id
    assert derived_instance.image == derived_id
    assert calls == [
        ("base", "base:tag"),
        (
            "derived",
            "derived:tag",
            derived.root,
            base_id,
        ),
    ]


def test_prepare_team_images_never_rebuilds_an_operator_override(
    tmp_path: Path,
    team_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = SimpleNamespace(image="custom:approved", image_override="custom:approved")
    binding = SimpleNamespace(team=load_team(team_repo), instance=selected)
    exact = "sha256:" + "c" * 64
    monkeypatch.setattr(
        "cyclo.cli.require_team_runtime_image",
        lambda image: exact if image == "custom:approved" else None,
    )
    monkeypatch.setattr(
        "cyclo.cli.ensure_team_runtime_image",
        lambda *_args, **_kwargs: pytest.fail("override was rebuilt"),
    )

    assert (
        _prepare_team_images(
            (binding,),
            base_image="base:tag",
        )
        is None
    )
    assert selected.image == exact


def test_run_preflights_every_team_then_starts_project_bindings(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    install_run_fakes(monkeypatch, tmp_path)
    events: list[tuple[object, ...]] = []
    lock_depth = 0
    original_locked = StateStore.locked

    @contextmanager
    def tracked_lock(store):
        nonlocal lock_depth
        with original_locked(store):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    monkeypatch.setattr(StateStore, "locked", tracked_lock)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "cyclo.cli.preflight_binding",
        lambda binding, _store, _docker: events.append(
            ("preflight", binding.instance.id, lock_depth)
        ),
    )

    def start(_args, binding, _source, _store, _docker):
        events.append(("start", binding.instance.id, lock_depth))
        binding.instance.port = 4100 + len(events)

    monkeypatch.setattr("cyclo.cli.start_binding_locked", start)

    result = main(["--state-root", str(tmp_path / "state"), "run", str(definition)])

    assert result == 0
    assert [event[0] for event in events] == [
        "preflight",
        "preflight",
        "start",
        "start",
    ]
    assert all(event[2] == 1 for event in events)
    starts = [event for event in events if event[0] == "start"]
    assert [event[1] for event in starts] == [
        "integration-project-review-team",
        "integration-project-review-audit",
    ]
    output = capsys.readouterr().out
    assert output.count("started Cyclo instance:") == 2
    assert "mount (rw): source" in output
    assert "mount (ro): documentation" in output


def test_run_materializes_each_teams_container_project(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    state_root = tmp_path / "state"
    store = StateStore(state_root)
    install_run_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr("cyclo.cli.agentws_root", packaged_agentws_root)
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    materialized: dict[str, str] = {}

    class FakeDocker:
        @staticmethod
        def previous_launch_lifecycle_state(_instance, *, system):
            assert system == store.system
            return DockerContainerState.ABSENT

        @staticmethod
        def container_lifecycle_active(_instance, *, system):
            assert system == store.system
            return True

        @staticmethod
        def ensure_network(_name, _identifier, *, system, offline):
            assert system == store.system
            assert offline is False

        @staticmethod
        def start(spec):
            config = spec.runtime_root / "project.cyclo"
            materialized[spec.instance.id] = config.read_text(encoding="utf-8")
            return 4100 + len(materialized)

        @staticmethod
        def wait_ready(_instance, _port, *, system, host):
            assert system == store.system
            assert host == "127.0.0.1"

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["--state-root", str(state_root), "run", str(definition)]) == 0

    assert set(materialized) == {
        "integration-project-review-team",
        "integration-project-review-audit",
    }
    first = materialized["integration-project-review-team"]
    second = materialized["integration-project-review-audit"]
    assert "team /team ro\n" in first
    assert "team /team rw\n" in second
    for config in (first, second):
        assert "mount source /workspace/source rw\n" in config
        assert (
            "mount documentation /readonly/documentation ro\n"
            in config
        )
        assert str(team_repo.resolve()) not in config
        assert str(second_team.resolve()) not in config
        assert str(project_repo.resolve()) not in config
    for identifier in materialized:
        runtime = store.runtime_root(identifier)
        assert (runtime / "project.cyclo").stat().st_mode & 0o777 == 0o444
        assert not (runtime / "PROJECT.md").exists()


def test_project_startup_interrupt_rolls_back_current_and_started_teams(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    store = StateStore(tmp_path / "state")
    install_run_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli.preflight_binding", lambda *_args: None)
    started: list[tuple[str, str]] = []
    stopped: list[tuple[str, str]] = []

    def start(_args, binding, _source, selected_store, _docker):
        selected_store.save(binding.instance)
        started.append((binding.instance.id, binding.instance.launch_id))
        if len(started) == 2:
            raise KeyboardInterrupt

    def stop(_args, _store, selected):
        stopped.append((selected.id, selected.launch_id))

    monkeypatch.setattr("cyclo.cli.start_binding_locked", start)
    monkeypatch.setattr(
        "cyclo.cli.stop_managed_instance_locked",
        lambda selected_store, _docker, selected: stop(
            None, selected_store, selected
        ),
    )

    result = main(["run", str(definition)])

    assert result == 130
    assert stopped == [started[1], started[0]]
    assert "interrupted" in capsys.readouterr().err


class GatewayDouble:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.status_value = gateway_status()
        self.store_volume = "cyclo-gateway-state"

    def providers(self) -> str:
        self.calls.append(("providers",))
        return "PROVIDER\tDESCRIPTION\nopenai\tOpenAI API"

    def status(self):
        self.calls.append(("status",))
        return self.status_value

    def login(self, arguments):
        self.calls.append(("login", tuple(arguments)))
        return self.status_value

    def build(self) -> str:
        self.calls.append(("build",))
        return "sha256:" + "c" * 64

    def start(self):
        self.calls.append(("start",))
        return self.status_value

    def restart(self):
        self.calls.append(("restart",))
        return self.status_value

    def refresh(self):
        self.calls.append(("refresh",))
        return self.status_value

    def stop(self) -> bool:
        self.calls.append(("stop",))
        return True

    def destroy_store(self) -> bool:
        self.calls.append(("destroy_store",))
        return True

    def usage(self):
        self.calls.append(("usage",))
        return {"requests": 3, "by_provider": {"openai": {"requests": 3}}}


@pytest.mark.parametrize(
    ("arguments", "call", "locks", "output"),
    (
        (("providers",), ("providers",), 1, "OpenAI API"),
        (("status",), ("status",), 0, "gateway\tready"),
        (("build",), ("refresh",), 1, "sha256:"),
        (("start",), ("start",), 1, "gateway\tready"),
        (("restart",), ("restart",), 1, "gateway\tready"),
        (("stop",), ("stop",), 1, "stopped gateway"),
        (
            ("destroy-store", "--confirm", "cyclo-gateway-state"),
            ("destroy_store",),
            1,
            "destroyed gateway store",
        ),
    ),
)
def test_gateway_actions_use_only_the_gateway_component(
    arguments: tuple[str, ...],
    call: tuple[object, ...],
    locks: int,
    output: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore(tmp_path / "state")
    proxy = GatewayDouble()
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: proxy)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: pytest.fail(
            "gateway command used provider composition"
        ),
    )

    result = main(["gateway", *arguments])

    assert result == 0
    assert proxy.calls == [call]
    assert store.lock_entries == locks
    rendered = capsys.readouterr().out
    assert output in rendered
    if arguments[0] in {"build", "start", "restart", "status"}:
        assert "store\tcyclo-gateway-state" in rendered


def test_destroy_store_requires_the_exact_volume_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore(tmp_path / "state")
    proxy = GatewayDouble()
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: proxy)

    result = main(["gateway", "destroy-store", "--confirm", "wrong-volume"])

    assert result == 1
    assert proxy.calls == []
    assert store.lock_entries == 1
    assert "--confirm must equal cyclo-gateway-state" in capsys.readouterr().err


def test_gateway_help_describes_store_and_credential_free_discovery() -> None:
    parser = build_parser()
    root_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    gateway = root_action.choices["gateway"]
    gateway_action = next(
        action for action in gateway._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert "credentials, subscriptions, and retained usage history" in gateway.format_help()
    providers_help = " ".join(gateway_action.choices["providers"].format_help().split())
    assert "Providers are upstream AI services" in providers_help
    assert "does not read or mount the gateway credential store" in providers_help
    login_help = " ".join(gateway_action.choices["login"].format_help().split())
    assert "catalogue provider/account name" in login_help
    assert "default: PROVIDER" in login_help
    destroy_help = " ".join(gateway_action.choices["destroy-store"].format_help().split())
    assert "--confirm VOLUME" in destroy_help
    assert "credentials, subscriptions, and retained usage history" in destroy_help
    assert "irreversibly" in destroy_help


def test_gateway_login_forwards_only_explicit_login_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore(tmp_path / "state")
    proxy = GatewayDouble()
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: proxy)

    result = main(
        [
            "gateway",
            "login",
            "openai",
            "--as",
            "work",
            "--api-key-env",
            "WORK_OPENAI_KEY",
        ]
    )

    assert result == 0
    assert proxy.calls == [
        ("login", ("openai", "--as", "work", "--api-key-env", "WORK_OPENAI_KEY"))
    ]
    assert store.lock_entries == 1
    assert "gateway\tready" in capsys.readouterr().out


def test_gateway_status_reports_stale_not_ready_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proxy = GatewayDouble()
    proxy.status_value = gateway_status(ready=False, current=False)
    monkeypatch.setattr(
        "cyclo.cli.state_store", lambda _args: RecordingStore(tmp_path / "state")
    )
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: proxy)

    assert main(["gateway", "status"]) == 1
    rendered = capsys.readouterr().out
    assert "gateway\tstale" in rendered
    assert "stale\tstale" not in rendered


class ProviderSystemDouble:
    def __init__(self, socket_path: Path) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.connection_value = provider_connection(socket_path)
        self.gateway = SimpleNamespace(
            socket_path=Path("/run/cyclo/gateway/component.sock")
        )
        self.configuration = SimpleNamespace(
            path=Path("/etc/cyclo/host.conf"),
            providers=(object(),),
        )

    def check(self) -> int:
        self.calls.append(("check",))
        return 1

    def build(self):
        self.calls.append(("build",))
        return (("pass", "sha256:" + "d" * 64),)

    def statuses(self):
        self.calls.append(("statuses",))
        return self.connection_value.components

    def status_component(self, name):
        self.calls.append(("status_component", name))
        return next(
            status
            for status in self.connection_value.components
            if status.name == name
        )

    def connection(self, statuses=None):
        self.calls.append(("connection",))
        if statuses is not None:
            assert statuses is self.connection_value.components
        return self.connection_value

    def catalogue(self, connection):
        self.calls.append(("catalogue",))
        assert connection is self.connection_value
        return (
            connection,
            {"models": [catalogue_model(model) for model in MODELS]},
        )

    def start(self):
        self.calls.append(("start",))
        return self.connection_value

    def restart(self):
        self.calls.append(("restart",))
        return self.connection_value

    def stop(self):
        self.calls.append(("stop",))
        return ("pass", "old-component")

def test_component_list_reports_each_component_without_failing_for_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    providers = ProviderSystemDouble(tmp_path / "component.sock")
    providers.connection_value = SimpleNamespace(
        generation="provider-generation",
        socket_path=tmp_path / "component.sock",
        components=(
            gateway_status(),
            component_status(
                name="broken",
                present=False,
                running=False,
                ready=False,
                error="build failed",
            ),
        ),
    )
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: providers,
    )

    assert main(["component", "list"]) == 0
    rendered = capsys.readouterr().out
    assert "gateway" in rendered
    assert "broken" in rendered
    assert "build failed" in rendered


def test_component_status_labels_an_inspection_failure_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    providers = ProviderSystemDouble(tmp_path / "component.sock")
    providers.connection_value = SimpleNamespace(
        generation="provider-generation",
        socket_path=tmp_path / "component.sock",
        components=(
            gateway_status(),
            component_status(
                name="broken",
                present=False,
                running=False,
                ready=False,
                container_state="unknown",
                error="cannot inspect ownership",
            ),
        ),
    )
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: providers,
    )

    assert main(["component", "status"]) == 1
    row = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("broken")
    )
    assert "unknown" in row
    assert "absent" not in row


def test_component_gateway_status_does_not_parse_host_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = ProviderSystemDouble(tmp_path / "component.sock")
    loads: list[bool] = []

    def make_system(_args, _store, *, load_config=True):
        loads.append(load_config)
        return providers

    monkeypatch.setattr("cyclo.cli.provider_system", make_system)

    assert main(["component", "status", "gateway"]) == 0
    assert loads == [False]


@pytest.mark.parametrize(
    ("arguments", "call", "locks", "output"),
    (
        (("check",), ("check",), 0, "ok: 1 provider component(s)"),
        (("build",), ("build",), 1, "pass\tsha256:"),
        (("status",), ("statuses",), 0, "passthrough"),
        (("start",), ("start",), 1, "gateway"),
        (("restart",), ("restart",), 1, "passthrough"),
    ),
)
def test_provider_actions_use_the_configured_stack(
    arguments: tuple[str, ...],
    call: tuple[object, ...],
    locks: int,
    output: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    socket_dir = tmp_path / "provider"
    socket_dir.mkdir()
    store = RecordingStore(tmp_path / "state")
    stack = ProviderSystemDouble(socket_dir / "component.sock")
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: stack,
    )

    assert main(["providers", *arguments]) == 0
    assert stack.calls == [call]
    assert store.lock_entries == locks
    assert output in capsys.readouterr().out


def test_provider_stop_does_not_parse_an_invalid_host_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    invalid = root / "host.conf"
    invalid.write_text("this is deliberately invalid\n", encoding="utf-8")
    store = RecordingStore(root)
    stack = ProviderSystemDouble(tmp_path / "component.sock")
    loads: list[bool] = []

    def make_stack(args, selected_store, *, load_config=True):
        assert selected_store is store
        assert host_config(args, selected_store) == invalid
        loads.append(load_config)
        return stack

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.provider_system", make_stack)

    result = main(["providers", "stop", "--state-root", str(root)])

    assert result == 0
    assert loads == [False]
    assert stack.calls == [("stop",)]
    assert store.lock_entries == 1
    assert "stopped: pass, old-component" in capsys.readouterr().out


def test_models_lists_only_the_outer_provider_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stack = ProviderSystemDouble(tmp_path / "component.sock")
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: stack,
    )
    monkeypatch.setattr(
        "cyclo.cli.gateway",
        lambda *_args: pytest.fail("models queried gateway directly"),
    )

    assert main(["models", "--state-root", str(tmp_path / "state")]) == 0
    assert capsys.readouterr().out.splitlines() == list(MODELS)
    assert stack.calls == [("start",), ("catalogue",)]


def test_models_explains_an_empty_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stack = ProviderSystemDouble(tmp_path / "component.sock")
    stack.catalogue = lambda connection: (
        connection,
        {"models": []},
    )
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: stack,
    )

    assert main(["models"]) == 1
    assert "cyclo gateway providers" in capsys.readouterr().err


def test_usage_is_the_gateway_global_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proxy = GatewayDouble()
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: proxy)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: pytest.fail(
            "usage queried provider composition"
        ),
    )

    assert main(["--state-root", str(tmp_path / "state"), "usage"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "requests": 3,
        "by_provider": {"openai": {"requests": 3}},
    }
    assert proxy.calls == [("usage",)]


@pytest.mark.parametrize("task_status", (0, 7))
def test_task_uses_agentws_and_always_removes_the_copied_spec(
    task_status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    project_file = tmp_path / "project.cyclo"
    project_file.write_text("not reread by task\n", encoding="utf-8")
    selected = instance("silicon-rtl", tmp_path, active=True, project_file=project_file)
    selected.project_mounts.append(
        {"name": "specifications", "path": "/host/specifications", "mode": "ro"}
    )
    persist(store, selected)
    specification = tmp_path / "task.md"
    specification.write_text("Create a UART.\n", encoding="utf-8")
    calls: list[tuple[object, ...]] = []

    class FakeDocker:
        def container_running(self, instance, *, system):
            assert system == store.system
            calls.append(("running", instance.container_name))
            return True

        def copy_to(self, instance, source, destination, *, system):
            assert system == store.system
            calls.append(("copy", instance.container_name, source, destination))

        def exec(self, instance, command, *, system, check=True, user=None):
            assert system == store.system
            calls.append(
                ("exec", instance.container_name, tuple(command), check, user)
            )
            return task_status if command[0] == "/agentws/bin/task-create" else 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    result = main(
        [
            "--state-root",
            str(store.root),
            "task",
            "run",
            selected.id,
            "uart",
            str(specification),
        ]
    )

    assert result == (0 if task_status == 0 else 1)
    create = next(call for call in calls if call[:2] == ("exec", selected.container_name))
    assert create[2][0] == "/agentws/bin/task-create"
    cleanup = [call for call in calls if call[0] == "exec" and call[2][:2] == ("rm", "-f")]
    assert len(cleanup) == 1
    assert cleanup[0][4] == "0:0"
    captured = capsys.readouterr()
    if task_status == 0:
        assert "source: /workspace/source" in captured.out
        assert "specifications: /readonly/specifications" in captured.out
    else:
        assert "AgentWS task creation failed with status 7" in captured.err


def test_task_cleanup_failure_does_not_mask_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("silicon-rtl", tmp_path, active=True)
    persist(store, selected)
    specification = tmp_path / "task.md"
    specification.write_text("Create a UART.\n", encoding="utf-8")

    class FakeDocker:
        def container_running(self, instance, *, system):
            return True

        def copy_to(self, instance, source, destination, *, system):
            return None

        def exec(self, instance, command, *, system, check=True, user=None):
            if command[0] == "/agentws/bin/task-create":
                return 7
            return 9

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(
        [
            "--state-root",
            str(store.root),
            "task",
            "run",
            selected.id,
            "uart",
            str(specification),
        ]
    ) == 1
    error = capsys.readouterr().err
    assert "AgentWS task creation failed with status 7" in error
    assert "copied task specification cleanup failed" in error
    assert "status 9" in error


def test_task_copy_failure_removes_any_partial_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("silicon-rtl", tmp_path, active=True)
    persist(store, selected)
    specification = tmp_path / "task.md"
    specification.write_text("Create a UART.\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    class FakeDocker:
        def container_running(self, instance, *, system):
            return True

        def copy_to(self, instance, source, destination, *, system):
            raise CycloError("injected copy failure")

        def exec(self, instance, command, *, system, check=True, user=None):
            commands.append(tuple(command))
            return 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(
        [
            "--state-root",
            str(store.root),
            "task",
            "run",
            selected.id,
            "uart",
            str(specification),
        ]
    ) == 1
    assert len(commands) == 1
    assert commands[0][:2] == ("rm", "-f")
    assert commands[0][2].startswith("/tmp/cyclo-task-uart-")
    assert "injected copy failure" in capsys.readouterr().err


def test_task_cleanup_failure_does_not_overturn_committed_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("silicon-rtl", tmp_path, active=True)
    persist(store, selected)
    specification = tmp_path / "task.md"
    specification.write_text("Create a UART.\n", encoding="utf-8")

    class FakeDocker:
        def container_running(self, instance, *, system):
            return True

        def copy_to(self, instance, source, destination, *, system):
            return None

        def exec(self, instance, command, *, system, check=True, user=None):
            if command[0] == "/agentws/bin/task-create":
                return 0
            return 9

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(
        [
            "--state-root",
            str(store.root),
            "task",
            "run",
            selected.id,
            "uart",
            str(specification),
        ]
    ) == 0
    captured = capsys.readouterr()
    assert f"project: {tmp_path.name}" in captured.out
    assert "copied task specification cleanup failed" in captured.err
    assert "status 9" in captured.err


@pytest.mark.parametrize(
    ("arguments", "agentws_command"),
    (
        (["list", "silicon-rtl"], ("/agentws/bin/task-list",)),
        (
            ["show", "silicon-rtl", "uart"],
            ("/agentws/bin/task-show", "uart"),
        ),
        (
            ["comment", "silicon-rtl", "uart", "check", "timing"],
            ("/agentws/bin/task-comment", "uart", "check timing"),
        ),
        (
            ["complete", "silicon-rtl", "uart", "-m", "accepted"],
            ("/agentws/bin/task-state", "uart", "done", "-m", "accepted"),
        ),
        (
            ["reopen", "silicon-rtl", "uart"],
            ("/agentws/bin/task-state", "uart", "open"),
        ),
    ),
)
def test_task_commands_are_a_scoped_agentws_interface(
    arguments: list[str],
    agentws_command: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("silicon-rtl", tmp_path, active=True)
    persist(store, selected)
    calls: list[tuple[object, ...]] = []

    class FakeDocker:
        def container_running(self, instance, *, system):
            assert system == store.system
            calls.append(("running", instance.container_name))
            return True

        def exec(self, instance, command, *, system, check=True, user=None):
            assert system == store.system
            calls.append(
                ("exec", instance.container_name, tuple(command), check, user)
            )
            return 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["--state-root", str(store.root), "task", *arguments]) == 0
    assert calls == [
        ("running", selected.container_name),
        ("exec", selected.container_name, agentws_command, False, None),
    ]


def test_task_rejects_a_foreign_same_name_container_before_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("silicon-rtl", tmp_path, active=True)
    persist(store, selected)
    docker = Docker()
    monkeypatch.setattr(
        docker,
        "_inspect_container",
        lambda _name: {
            "Id": "foreign-container-id",
            "Config": {
                "Labels": {
                    "io.cyclo.system": "ba9876543210",
                    "io.cyclo.kind": "team",
                    "io.cyclo.instance": selected.id,
                }
            },
            "State": {"Running": True},
        },
    )
    monkeypatch.setattr(
        docker,
        "_run",
        lambda *_args, **_kwargs: pytest.fail(
            "foreign container must never receive an AgentWS command"
        ),
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: docker)

    assert main(
        ["--state-root", str(store.root), "task", "list", selected.id]
    ) == 1
    assert "refusing to use non-Cyclo container" in capsys.readouterr().err


def test_task_rejects_an_agentws_invalid_id_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    persist(store, instance("silicon-rtl", tmp_path, active=True))
    monkeypatch.setattr(
        "cyclo.cli.Docker",
        lambda: pytest.fail("Docker consulted for an invalid task ID"),
    )

    assert main(
        ["--state-root", str(store.root), "task", "show", "silicon-rtl", ".hidden"]
    ) == 1
    assert "task ID must start with a letter or number" in capsys.readouterr().err


def test_stop_by_instance_id_does_not_consult_project_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("alpha", tmp_path, active=True)
    persist(store, selected)
    stopped: list[str] = []
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda _args, _store, instance: stopped.append(instance.id),
    )

    assert main(["--state-root", str(store.root), "stop", selected.id]) == 0
    assert stopped == [selected.id]
    assert "stopped Cyclo instance: alpha" in capsys.readouterr().out


def test_stop_project_uses_persisted_bindings_and_continues_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    definition = tmp_path / "deleted-project.cyclo"
    first = instance("alpha", tmp_path, active=True, project_file=definition)
    second = instance("beta", tmp_path, active=True, project_file=definition)
    persist(store, first)
    persist(store, second)
    attempted: list[str] = []

    def stop(_args, _store, instance):
        attempted.append(instance.id)
        if instance.id == first.id:
            raise CycloError("injected cleanup failure")

    monkeypatch.setattr("cyclo.cli.stop_instance", stop)

    result = main(["--state-root", str(store.root), "stop", str(definition)])

    assert result == 1
    assert attempted == [first.id, second.id]
    captured = capsys.readouterr()
    assert "stopped Cyclo instance: beta" in captured.out
    assert "project stop incomplete" in captured.err
    assert "injected cleanup failure" in captured.err


def test_forget_removes_only_a_confirmed_stopped_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("retired", tmp_path, active=False)
    persist(store, selected)
    task = store.tasks_dir(selected.id) / "saved-task"
    task.mkdir(parents=True)
    (task / "spec.md").write_text("durable work\n", encoding="utf-8")
    calls: list[tuple[object, ...]] = []

    class FakeDocker:
        def container_lifecycle_state(self, instance, *, system):
            assert instance.id == selected.id
            assert system == store.system
            return DockerContainerState.STOPPED

        def stop_remove(
            self,
            container,
            expected_instance,
            *,
            expected_system,
            expected_launch,
        ):
            calls.append(
                (
                    "container",
                    container,
                    expected_instance,
                    expected_system,
                    expected_launch,
                )
            )
            return True

        def remove_network(self, name, expected_instance, *, system):
            calls.append(("network", name, expected_instance, system))

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    arguments = [
        "--state-root",
        str(store.root),
        "forget",
        selected.id,
        "--confirm",
        selected.id,
    ]
    assert main(arguments) == 0
    assert not store.instance_dir(selected.id).exists()
    assert calls == [
        (
            "container",
            selected.container_name,
            selected.id,
            store.system,
            selected.launch_id,
        ),
        ("network", selected.network_name, selected.id, store.system),
    ]
    assert "forgot Cyclo instance: retired" in capsys.readouterr().out


def test_forget_requires_exact_confirmation_and_an_inactive_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("active", tmp_path, active=True)
    persist(store, selected)
    monkeypatch.setattr(
        "cyclo.cli.Docker",
        lambda: pytest.fail("unconfirmed state must not touch Docker"),
    )

    prefix = ["--state-root", str(store.root), "forget", selected.id]
    assert main([*prefix, "--confirm", "another"]) == 1
    assert "--confirm must exactly match" in capsys.readouterr().err

    class NoDockerCalls:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected Docker call: {name}")

    monkeypatch.setattr("cyclo.cli.Docker", NoDockerCalls)
    assert main([*prefix, "--confirm", selected.id]) == 1
    assert "still active" in capsys.readouterr().err
    assert store.metadata_path(selected.id).is_file()


@pytest.mark.parametrize(
    "state",
    [
        DockerContainerState.RUNNING,
        DockerContainerState.PAUSED,
        DockerContainerState.RESTARTING,
    ],
)
def test_forget_refuses_a_lifecycle_active_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: DockerContainerState,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = instance("retired", tmp_path, active=False)
    persist(store, selected)

    class FakeDocker:
        @staticmethod
        def container_lifecycle_state(instance, *, system):
            assert instance.id == selected.id
            assert system == store.system
            return state

        def __getattr__(self, name):
            raise AssertionError(f"unexpected Docker mutation: {name}")

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(
        [
            "--state-root",
            str(store.root),
            "forget",
            selected.id,
            "--confirm",
            selected.id,
        ]
    ) == 1
    assert f"container is still {state.value}" in capsys.readouterr().err
    assert store.metadata_path(selected.id).is_file()


def test_repair_marks_stale_records_and_removes_inactive_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = instance("stale", tmp_path, active=True)
    inactive = instance("inactive", tmp_path, active=False)
    store = RecordingStore(tmp_path / "state", [stale, inactive])
    calls: list[tuple[str, str]] = []

    def inspect(selected_store, _docker, *, stale: list[Instance]):
        assert selected_store is store
        store.instances[0].active = False
        stale.append(store.instances[0])
        return []

    class FakeDocker:
        @staticmethod
        def stop_remove(
            name, identifier, *, expected_system, expected_launch
        ):
            assert expected_system == store.system
            assert expected_launch == "0" * 32
            calls.append(("container", f"{name}:{identifier}"))
            return True

        @staticmethod
        def remove_network(name, identifier, *, system):
            assert system == store.system
            assert identifier in {"stale", "inactive"}
            calls.append(("network", name))

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.active_instances", inspect)
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["repair"]) == 0
    assert store.lock_entries == 1
    assert {call[1] for call in calls if call[0] == "container"} == {
        "cyclo-stale:stale",
        "cyclo-inactive:inactive",
    }
    assert {call[1] for call in calls if call[0] == "network"} == {
        "cyclo-stale-net",
        "cyclo-inactive-net",
    }
    assert "repaired 1 stale record(s); removed 2 inactive container(s)" in capsys.readouterr().out


def test_ps_distinguishes_container_and_metadata_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    running = instance("running", tmp_path, active=True)
    running.project_name = "live-project"
    orphan = instance("orphan", tmp_path, active=False)
    paused = instance("paused", tmp_path, active=True)
    restarting = instance("restarting", tmp_path, active=True)
    stale = instance("stale", tmp_path, active=True)
    stopped = instance("stopped", tmp_path, active=False)
    store = RecordingStore(
        tmp_path / "state",
        [running, orphan, paused, restarting, stale, stopped],
    )
    container_states = {
        running.container_name: DockerContainerState.RUNNING,
        orphan.container_name: DockerContainerState.RUNNING,
        paused.container_name: DockerContainerState.PAUSED,
        restarting.container_name: DockerContainerState.RESTARTING,
        stale.container_name: DockerContainerState.STOPPED,
        stopped.container_name: DockerContainerState.STOPPED,
    }
    shared_reads: list[bool] = []

    class FakeDocker:
        @staticmethod
        def container_lifecycle_state(instance, *, system):
            assert system == store.system
            return container_states[instance.container_name]

    def shared(_args, selected_store):
        assert selected_store is store
        shared_reads.append(True)
        return ProviderHealth("ready"), SimpleNamespace()

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr("cyclo.cli._shared_provider_health", shared)
    monkeypatch.setattr(
        "cyclo.cli.instance_provider_health",
        lambda *_args: ProviderHealth("ready"),
    )
    monkeypatch.setattr(
        "cyclo.cli.read_agent_supervisor_status",
        lambda _root: AgentSupervisorStatus(),
    )

    assert main(["ps"]) == 0
    rows = {
        fields[0]: fields
        for line in capsys.readouterr().out.splitlines()[1:]
        if (fields := line.split())
    }
    assert rows["running"][1:3] == ["running", "ready"]
    assert rows["running"][4] == "live-project"
    assert rows["orphan"][1:3] == ["orphan", "inactive"]
    assert rows["paused"][1:3] == ["paused", "inactive"]
    assert rows["restarting"][1:3] == ["restarting", "inactive"]
    assert rows["stale"][1:3] == ["stale", "inactive"]
    assert rows["stopped"][1:3] == ["stopped", "inactive"]
    assert shared_reads == [True]


def test_ps_reports_one_uninspectable_container_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = instance("broken", tmp_path, active=True)
    stopped = instance("stopped", tmp_path, active=False)
    store = RecordingStore(tmp_path / "state", [broken, stopped])

    class FakeDocker:
        @staticmethod
        def container_lifecycle_state(selected, *, system):
            assert system == store.system
            if selected.id == "broken":
                raise CycloError("container ownership labels do not match")
            return DockerContainerState.STOPPED

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli._shared_provider_health",
        lambda *_args: pytest.fail("no running instance should inspect providers"),
    )

    assert main(["ps"]) == 0
    output = capsys.readouterr().out
    assert "broken" in output
    assert "unknown (container ownership labels do not match)" in output
    assert "stopped" in output


def test_inspect_explains_one_persisted_instance_without_requiring_it_to_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = StateStore(tmp_path / "state")
    project_file = tmp_path / "project.cyclo"
    project_file.write_text("persisted definition path only\n", encoding="utf-8")
    selected = instance("silicon-rtl", tmp_path, project_file=project_file)
    selected.project_mounts.append(
        {"name": "specifications", "path": "/host/specifications", "mode": "ro"}
    )
    persist(store, selected)

    class FakeDocker:
        def container_lifecycle_state(self, instance, *, system):
            assert system == store.system
            assert instance.container_name == selected.container_name
            return DockerContainerState.STOPPED

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: pytest.fail("stopped instance queried providers"),
    )

    assert main(["inspect", selected.id, "--state-root", str(store.root)]) == 0
    rendered = capsys.readouterr().out
    assert "instance: silicon-rtl" in rendered
    assert "state: stopped" in rendered
    assert "source (rw):" in rendered
    assert "-> /workspace/source" in rendered
    assert "specifications (ro): /host/specifications -> /readonly/specifications" in rendered


def test_doctor_reports_a_ready_installation_without_mutating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore(tmp_path / "state")
    components = tmp_path / "components"
    stack = ProviderSystemDouble(tmp_path / "provider" / "component.sock")
    stack.configuration = SimpleNamespace(
        path=tmp_path / "host.conf",
        providers=(object(),),
    )
    class FakeDocker:
        @staticmethod
        def available():
            return True, "test-engine"

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: tmp_path / "agentws")
    monkeypatch.setattr("cyclo.cli.component_sources_root", lambda: components)
    monkeypatch.setattr(
        "cyclo.cli.parse_declaration",
        lambda _path: SimpleNamespace(provides=(COMPONENT_INTERFACE, PROVIDER_INTERFACE)),
    )
    monkeypatch.setattr("cyclo.cli.ComponentController", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: stack,
    )

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "ok  bundled AgentWS ABI:" in output
    assert "ok  component ABI:" in output
    assert "ok  persisted instance state: 0 instance(s)" in output
    assert "ok  Docker daemon: test-engine" in output
    assert "ok  host provider configuration:" in output
    assert "ok  credential gateway: ready" in output
    assert "ok  provider component pass: ready" in output
    assert "ok  selected provider catalogue: 2 model(s)" in output
    assert store.lock_entries == 0


def test_doctor_reports_a_catalogue_fallback_as_component_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore(tmp_path / "state")
    stack = ProviderSystemDouble(tmp_path / "provider" / "component.sock")
    stack.configuration = SimpleNamespace(
        path=tmp_path / "host.conf",
        providers=(object(),),
    )
    fallback = provider_connection(stack.gateway.socket_path)
    fallback.components = (
        fallback.components[0],
        component_status(
            name="pass",
            error="model catalogue unavailable: invalid response",
        ),
    )
    stack.catalogue = lambda _connection: (
        fallback,
        {"models": [catalogue_model(MODELS[0])]},
    )

    class FakeDocker:
        @staticmethod
        def available():
            return True, "test-engine"

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: tmp_path / "agentws")
    monkeypatch.setattr(
        "cyclo.cli.component_sources_root",
        lambda: tmp_path / "components",
    )
    monkeypatch.setattr(
        "cyclo.cli.parse_declaration",
        lambda _path: SimpleNamespace(
            provides=(COMPONENT_INTERFACE, PROVIDER_INTERFACE)
        ),
    )
    monkeypatch.setattr("cyclo.cli.ComponentController", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: stack,
    )

    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "no  provider component pass:" in output
    assert "model catalogue unavailable: invalid response" in output
    assert "ok  selected provider catalogue: 1 model(s)" in output


def test_doctor_stops_cleanly_when_docker_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore(tmp_path / "state")

    class MissingDocker:
        @staticmethod
        def available():
            return False, "daemon unavailable"

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: tmp_path / "agentws")
    monkeypatch.setattr(
        "cyclo.cli.component_sources_root", lambda: tmp_path / "components"
    )
    monkeypatch.setattr(
        "cyclo.cli.parse_declaration",
        lambda _path: SimpleNamespace(provides=(COMPONENT_INTERFACE, PROVIDER_INTERFACE)),
    )
    monkeypatch.setattr("cyclo.cli.ComponentController", MissingDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_system",
        lambda *_args, **_kwargs: pytest.fail("doctor parsed providers without Docker"),
    )

    assert main(["doctor"]) == 1
    assert "no  Docker daemon: daemon unavailable" in capsys.readouterr().out
    assert store.lock_entries == 0
