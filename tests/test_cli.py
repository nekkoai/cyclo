from __future__ import annotations

import argparse
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.agentws_queue import AgentSupervisorStatus
from cyclo.cli import _prepare_team_images, build_parser, main
from cyclo.component_stack import COMPONENT_INTERFACE, PROVIDER_INTERFACE
from cyclo.errors import CycloError
from cyclo.health import ProviderHealth
from cyclo.installation import (
    derived_team_image_name,
    installation_id,
    team_container_name,
    team_image_name,
    team_network_name,
)
from cyclo.project import load_project
from cyclo.state import Instance, StateStore
from cyclo.team import load_team


MODELS = ("openai-codex/gpt-test", "anthropic/claude-test")


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


def docker_status(
    *,
    present: bool = True,
    running: bool = True,
    current: bool = True,
    lifecycle: str = "running",
) -> SimpleNamespace:
    return SimpleNamespace(
        image_id="sha256:" + "a" * 64,
        container_id="b" * 64 if present else None,
        running=running,
        current=current,
        lifecycle=lifecycle,
        engine_health="healthy" if running else "none",
    )


def gateway_status(*, ready: bool = True, current: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        ready=ready,
        docker=docker_status(current=current),
        socket_path=Path("/run/cyclo/gateway/component.sock"),
    )


def stack_status(socket_path: Path, *, ready: bool = True) -> SimpleNamespace:
    component = SimpleNamespace(
        instance="pass",
        ready=ready,
        docker=docker_status(),
    )
    return SimpleNamespace(
        generation="provider-generation",
        provider_socket_path=socket_path,
        gateway=gateway_status(ready=ready),
        components=(component,),
        ready=ready,
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


class RunStack:
    def __init__(self, socket_path: Path) -> None:
        self.status_value = stack_status(socket_path)
        self.provider_socket_path = socket_path
        self.assembly = SimpleNamespace(generation="provider-generation")
        self.calls: list[str] = []

    def require_ready(self):
        self.calls.append("require_ready")
        return self.status_value

    def models_document(self):
        self.calls.append("models_document")
        return {"models": [{"id": model} for model in MODELS]}


def install_run_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> RunStack:
    source = tmp_path / "agentws"
    source.mkdir()
    socket_dir = tmp_path / "provider-socket"
    socket_dir.mkdir()
    stack = RunStack(socket_dir / "component.sock")
    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: source)
    monkeypatch.setattr("cyclo.cli.provider_stack", lambda *_args, **_kwargs: stack)
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
        "ps",
        "inspect",
        "dashboard",
        "task",
        "logs",
        "path",
        "usage",
        "models",
        "repair",
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
    arguments = [
        "project",
        "init",
        str(definition),
        "--description",
        "RTL integration project",
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
    assert project.teams[0].path == team_repo.resolve()
    assert project.mounts[0].path == source.resolve()
    assert "next: cyclo validate" in capsys.readouterr().out

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


def test_refresh_builds_before_stopping_and_restarts_active_projects(
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

    class RefreshGateway:
        @staticmethod
        def build():
            events.append("gateway-build")

        @staticmethod
        def restart(*, build):
            assert build is False
            events.append("gateway-restart")

    class RefreshStack:
        @staticmethod
        def build():
            events.append("providers-build")

        @staticmethod
        def restart(*, build):
            assert build is False
            events.append("providers-restart")

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: object())
    monkeypatch.setattr(
        "cyclo.cli.active_instances",
        lambda selected_store, _docker: list(selected_store.instances),
    )
    monkeypatch.setattr("cyclo.cli.gateway", lambda _args, _store: RefreshGateway())
    monkeypatch.setattr("cyclo.cli.provider_stack", lambda _args, _store: RefreshStack())
    monkeypatch.setattr(
        "cyclo.cli.ensure_team_runtime_image",
        lambda image, *, build: events.append(f"image-build:{image}:{build}")
        or "sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        "cyclo.cli.ensure_derived_team_image",
        lambda image, _root, base, *, build: events.append(
            f"derived-build:{image}:{base}:{build}"
        )
        or "sha256:" + "b" * 64,
    )
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda _args, _store, identifier: events.append(f"stop:{identifier}"),
    )

    def run(project_args):
        assert project_args.project == str(definition)
        assert project_args.image is None
        assert project_args.offline is True
        assert project_args.host == "0.0.0.0"
        assert project_args.port == 4317
        assert project_args.build is False
        events.append(f"run:{project_args.project}")
        return 0

    monkeypatch.setattr("cyclo.cli.cmd_run", run)

    assert main(["refresh"]) == 0
    stopped = "stop:integration-project-review-team"
    assert events.index("gateway-build") < events.index(stopped)
    assert events.index("providers-build") < events.index(stopped)
    assert events.index(stopped) < events.index("gateway-restart")
    assert events.index("gateway-restart") < events.index("providers-restart")
    assert events.index("providers-restart") < events.index(f"run:{definition}")
    assert (
        f"image-build:{team_image_name(store.system, '0.2.0')}:True"
        in events
    )
    derived = derived_team_image_name(
        store.system,
        "0.2.0",
        team_repo,
        team_repo.name,
    )
    derived_event = f"derived-build:{derived}:sha256:{'a' * 64}:True"
    assert derived_event in events
    assert events.index(derived_event) < events.index(stopped)
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
    assert output.count("CYCLO_PROJECT_MANIFEST=/agentws/PROJECT.md") == 2
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


def test_run_rejects_build_with_an_operator_managed_image(
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

    assert (
        main(
            [
                "run",
                "--dry-run",
                "--build",
                "--image",
                "custom:approved",
                str(definition),
            ]
        )
        == 1
    )
    assert "cannot rebuild an operator-supplied --image" in capsys.readouterr().err


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
        lambda image, *, build: calls.append(("base", image, build)) or base_id,
    )
    monkeypatch.setattr(
        "cyclo.cli.ensure_derived_team_image",
        lambda image, root, base, *, build: calls.append(
            ("derived", image, root, base, build)
        )
        or derived_id,
    )

    assert (
        _prepare_team_images(
            bindings,
            base_image="base:tag",
            build=False,
        )
        == base_id
    )
    assert plain_instance.image == base_id
    assert derived_instance.image == derived_id
    assert calls == [
        ("base", "base:tag", False),
        (
            "derived",
            "derived:tag",
            derived.root,
            base_id,
            False,
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
            build=False,
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
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "cyclo.cli.preflight_binding",
        lambda binding, _store, _docker: events.append(("preflight", binding.instance.id)),
    )

    def start(_args, binding, _source, _store, _docker):
        events.append(("start", binding.instance.id))
        binding.instance.port = 4100 + len(events)

    monkeypatch.setattr("cyclo.cli.start_binding", start)

    result = main(
        ["--state-root", str(tmp_path / "state"), "run", "--build", str(definition)]
    )

    assert result == 0
    assert [event[0] for event in events] == [
        "preflight",
        "preflight",
        "start",
        "start",
    ]
    starts = [event for event in events if event[0] == "start"]
    assert [event[1] for event in starts] == [
        "integration-project-review-team",
        "integration-project-review-audit",
    ]
    output = capsys.readouterr().out
    assert output.count("started Cyclo instance:") == 2
    assert "mount (rw): source" in output
    assert "mount (ro): documentation" in output


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
    stopped: list[tuple[str, str | None]] = []

    def start(_args, binding, _source, selected_store, _docker):
        selected_store.save(binding.instance)
        started.append((binding.instance.id, binding.instance.launch_id))
        if len(started) == 2:
            raise KeyboardInterrupt

    def stop(_args, _store, identifier, *, expected_launch_id=None):
        stopped.append((identifier, expected_launch_id))

    monkeypatch.setattr("cyclo.cli.start_binding", start)
    monkeypatch.setattr("cyclo.cli.stop_instance", stop)

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

    def login(self, arguments) -> None:
        self.calls.append(("login", tuple(arguments)))

    def build(self) -> str:
        self.calls.append(("build",))
        return "sha256:" + "c" * 64

    def start(self):
        self.calls.append(("start",))
        return self.status_value

    def restart(self, *, build: bool = False):
        self.calls.append(("restart", build))
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
        (("providers",), ("providers",), 0, "OpenAI API"),
        (("status",), ("status",), 0, "gateway\tready"),
        (("build",), ("build",), 1, "sha256:"),
        (("start",), ("start",), 1, "gateway\tready"),
        (("restart", "--build"), ("restart", True), 1, "gateway\tready"),
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
        "cyclo.cli.provider_stack",
        lambda *_args, **_kwargs: pytest.fail("gateway command used provider stack"),
    )

    result = main(["gateway", *arguments])

    assert result == 0
    assert proxy.calls == ([call, ("restart", False)] if arguments == ("build",) else [call])
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
    assert "restart the gateway" in capsys.readouterr().out


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


class ProviderStackDouble:
    def __init__(self, socket_path: Path) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.status_value = stack_status(socket_path)

    def check(self) -> int:
        self.calls.append(("check",))
        return 1

    def build(self):
        self.calls.append(("build",))
        return (("pass", "sha256:" + "d" * 64),)

    def status(self):
        self.calls.append(("status",))
        return self.status_value

    def start(self):
        self.calls.append(("start",))
        return self.status_value

    def restart(self, *, build: bool = False):
        self.calls.append(("restart", build))
        return self.status_value

    def stop(self):
        self.calls.append(("stop",))
        return ("pass", "old-component")

    def model_ids(self):
        self.calls.append(("model_ids",))
        return MODELS


@pytest.mark.parametrize(
    ("arguments", "call", "locks", "output"),
    (
        (("check",), ("check",), 0, "ok: 1 provider component(s)"),
        (("build",), ("build",), 1, "pass\tsha256:"),
        (("status",), ("status",), 0, "pass\tready"),
        (("start",), ("start",), 1, "gateway\tready"),
        (("restart", "--build"), ("restart", True), 1, "pass\tready"),
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
    stack = ProviderStackDouble(socket_dir / "component.sock")
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.provider_stack", lambda *_args, **_kwargs: stack)

    assert main(["providers", *arguments]) == 0
    assert stack.calls == [call]
    assert store.lock_entries == locks
    assert output in capsys.readouterr().out


def test_provider_stop_does_not_parse_an_invalid_host_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "host.conf"
    invalid.write_text("this is deliberately invalid\n", encoding="utf-8")
    store = RecordingStore(tmp_path / "state")
    stack = ProviderStackDouble(tmp_path / "component.sock")
    loads: list[bool] = []

    def make_stack(args, selected_store, *, load_config=True):
        assert selected_store is store
        assert Path(args.host_config) == invalid
        loads.append(load_config)
        return stack

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.provider_stack", make_stack)

    result = main(["providers", "stop", "--host-config", str(invalid)])

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
    stack = ProviderStackDouble(tmp_path / "component.sock")
    monkeypatch.setattr("cyclo.cli.provider_stack", lambda *_args, **_kwargs: stack)
    monkeypatch.setattr(
        "cyclo.cli.gateway",
        lambda *_args: pytest.fail("models queried gateway directly"),
    )

    assert main(["models", "--state-root", str(tmp_path / "state")]) == 0
    assert capsys.readouterr().out.splitlines() == list(MODELS)
    assert stack.calls == [("model_ids",)]


def test_models_explains_an_empty_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stack = ProviderStackDouble(tmp_path / "component.sock")
    stack.model_ids = lambda: ()
    monkeypatch.setattr("cyclo.cli.provider_stack", lambda *_args, **_kwargs: stack)

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
        "cyclo.cli.provider_stack",
        lambda *_args, **_kwargs: pytest.fail("usage queried provider stack"),
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
        def container_running(self, name):
            calls.append(("running", name))
            return True

        def copy_to(self, container, source, destination):
            calls.append(("copy", container, source, destination))

        def exec(self, container, command, *, check=True, user=None):
            calls.append(("exec", container, tuple(command), check, user))
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
        def container_running(self, name):
            calls.append(("running", name))
            return True

        def exec(self, container, command, *, check=True, user=None):
            calls.append(("exec", container, tuple(command), check, user))
            return 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["--state-root", str(store.root), "task", *arguments]) == 0
    assert calls == [
        ("running", selected.container_name),
        ("exec", selected.container_name, agentws_command, False, None),
    ]


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
        lambda _args, _store, identifier: stopped.append(identifier),
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

    def stop(_args, _store, identifier):
        attempted.append(identifier)
        if identifier == first.id:
            raise CycloError("injected cleanup failure")

    monkeypatch.setattr("cyclo.cli.stop_instance", stop)

    result = main(["--state-root", str(store.root), "stop", str(definition)])

    assert result == 1
    assert attempted == [first.id, second.id]
    captured = capsys.readouterr()
    assert "stopped Cyclo instance: beta" in captured.out
    assert "project stop incomplete" in captured.err
    assert "injected cleanup failure" in captured.err


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
        def container_exists(_name):
            return True

        @staticmethod
        def stop_remove(name, identifier, *, expected_system):
            assert expected_system == store.system
            calls.append(("container", f"{name}:{identifier}"))

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
    stale = instance("stale", tmp_path, active=True)
    stopped = instance("stopped", tmp_path, active=False)
    store = RecordingStore(tmp_path / "state", [running, orphan, stale, stopped])
    running_containers = {running.container_name, orphan.container_name}
    shared_reads: list[bool] = []

    class FakeDocker:
        @staticmethod
        def container_running(name):
            return name in running_containers

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
    assert rows["stale"][1:3] == ["stale", "inactive"]
    assert rows["stopped"][1:3] == ["stopped", "inactive"]
    assert shared_reads == [True]


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
        def container_running(self, name):
            assert name == selected.container_name
            return False

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_stack",
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
    stack = ProviderStackDouble(tmp_path / "provider" / "component.sock")
    stack.assembly = SimpleNamespace(path=tmp_path / "host.conf", providers=(object(),))
    stack.model_ids = lambda: MODELS

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
    monkeypatch.setattr("cyclo.cli.ComponentDocker", FakeDocker)
    monkeypatch.setattr("cyclo.cli.provider_stack", lambda *_args, **_kwargs: stack)

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "ok  bundled AgentWS ABI:" in output
    assert "ok  component ABI:" in output
    assert "ok  persisted instance state: 0 instance(s)" in output
    assert "ok  Docker daemon: test-engine" in output
    assert "ok  host provider configuration:" in output
    assert "ok  credential gateway: current and ready" in output
    assert "ok  provider component pass: ready" in output
    assert "ok  outer provider catalogue: 2 model(s)" in output
    assert store.lock_entries == 0


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
    monkeypatch.setattr("cyclo.cli.ComponentDocker", MissingDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_stack",
        lambda *_args, **_kwargs: pytest.fail("doctor parsed providers without Docker"),
    )

    assert main(["doctor"]) == 1
    assert "no  Docker daemon: daemon unavailable" in capsys.readouterr().out
    assert store.lock_entries == 0
