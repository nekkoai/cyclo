from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from cyclo.agentws_bundle import packaged_agentws_template
from cyclo.cli import (
    DEFAULT_GATEWAY_IMAGE,
    _DashboardUsageReader,
    _task_project_summary,
    build_parser,
    cmd_provider,
    cmd_repair,
    cmd_runtime,
    main,
    stop_instance,
)
from cyclo.project_run import (
    RunBinding,
    binding_matches as _binding_matches,
    capture_source_identities as _capture_source_identities,
    start_binding as _start_binding,
    validate_running_mount_boundaries as _validate_running_mount_boundaries,
    verify_source_identities as _verify_source_identities,
)
from cyclo.errors import CycloError
from cyclo.docker import DockerContainerState
from cyclo.project import ProjectMount, load_project, render_project_manifest
from cyclo.state import Instance, StateStore
from cyclo.team import load_team


def write_project_definition(
    path: Path,
    team_repo: Path,
    project_repo: Path,
    *,
    second_team: Path | None = None,
) -> Path:
    docs = path.parent / "specifications"
    docs.mkdir(exist_ok=True)
    lines = [
        "name integration-project",
        "description Exercise multiple Cyclo teams and named mounts.",
        f"team {team_repo} ro",
    ]
    if second_team is not None:
        lines.append(f"team {second_team} rw")
    lines.extend(
        [
            f"mount source {project_repo} rw",
            f"mount specifications {docs} ro",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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
    assert "cyclo.launch=" not in output
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
    assert "project root writable" in help_text
    assert "--project-read-only" not in help_text
    assert "--host" in help_text
    assert "default: 127.0.0.1" in help_text
    assert "0.0.0.0" in help_text
    assert "/workspace" not in help_text
    assert "/team" not in help_text


def test_run_defaults_agentws_to_loopback() -> None:
    args = build_parser().parse_args(["run", "team", "project"])

    assert args.host == "127.0.0.1"


def test_project_file_dry_run_expands_every_team_and_named_mount_without_state(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project_definition(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    state = tmp_path / "state"

    result = main(
        [
            "--state-root",
            str(state),
            "run",
            "--dry-run",
            str(definition),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert output.count("docker run --detach") == 2
    assert "# instance integration-project-review-team" in output
    assert "# instance integration-project-review-audit" in output
    assert output.count(f"src={project_repo},dst=/workspace/source") == 2
    assert output.count("dst=/readonly/specifications,readonly") == 2
    assert f"src={team_repo},dst=/team,readonly" in output
    assert f"src={second_team},dst=/team" in output
    assert f"src={second_team},dst=/team,readonly" not in output
    assert output.count("CYCLO_PROJECT_MANIFEST=/agentws/PROJECT.md") == 2
    assert output.count("CYCLO_PROVIDER_RUNTIME_HEALTH_URL=http://") == 2
    assert output.count(":8788/health") == 2
    assert not state.exists()


def test_validate_project_file_reports_teams_and_mounts(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    definition = write_project_definition(
        tmp_path / "project.cyclo", team_repo, project_repo
    )

    result = main(["validate", str(definition)])

    output = capsys.readouterr().out
    assert result == 0
    assert "project: integration-project" in output
    assert "team (ro): review-team" in output
    assert "mount (rw): source" in output
    assert "-> /workspace/source" in output


def test_validate_preserves_team_directories_with_cyclo_suffix(
    tmp_path: Path, team_repo: Path, capsys
) -> None:
    suffixed_team = tmp_path / "review.cyclo"
    shutil.copytree(team_repo, suffixed_team)

    assert main(["validate", str(suffixed_team)]) == 0
    assert "team: review.cyclo" in capsys.readouterr().out


@pytest.mark.parametrize("option", ["--name=override", "--team-write"])
def test_project_file_rejects_legacy_authority_overrides(
    option: str,
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    definition = write_project_definition(
        tmp_path / "project.cyclo", team_repo, project_repo
    )

    result = main(["run", "--dry-run", option, str(definition)])

    assert result == 1
    assert "cannot be used with project.cyclo" in capsys.readouterr().err


def test_project_file_preflights_every_team_before_printing_any_run(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    capsys,
) -> None:
    non_repository = tmp_path / "not-a-repository"
    shutil.copytree(team_repo, non_repository, ignore=shutil.ignore_patterns(".git"))
    definition = write_project_definition(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=non_repository,
    )

    result = main(["run", "--dry-run", str(definition)])

    captured = capsys.readouterr()
    assert result == 1
    assert "team is not a Git repository" in captured.err
    assert "docker run" not in captured.out


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
    assert "mount overlaps host provider configuration" in capsys.readouterr().err


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

    instance.legacy_project_read_only = True
    store.save(instance)
    calls.clear()
    result = main(
        [
            "--state-root",
            str(store.root),
            "task",
            "alpha",
            "blocked-legacy-task",
            str(spec),
        ]
    )
    assert result == 1
    assert calls == []
    assert "stop and rerun it" in capsys.readouterr().err


def test_task_reports_named_project_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    project_file = tmp_path / "project.cyclo"
    project_file.write_text("not read by task\n", encoding="utf-8")
    project = Instance(
        id="silicon-rtl",
        team_name="rtl",
        team_path="/teams/rtl",
        project_path=str(tmp_path),
        generation="team-generation",
        providers=["codex"],
        models=["codex/model"],
        container_name="cyclo-silicon-rtl",
        network_name="cyclo-silicon-rtl-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=False,
        project_name="silicon",
        project_file=str(project_file),
        project_description="RTL development",
        project_generation="project-generation",
        project_mounts=[
            {
                "name": "source",
                "path": "/host/core-et",
                "mode": "rw",
            },
            {
                "name": "specifications",
                "path": "/host/specifications",
                "mode": "ro",
            },
        ],
    )
    store.save(project)
    spec = tmp_path / "spec.md"
    spec.write_text("Create a UART.\n", encoding="utf-8")

    class FakeDocker:
        @staticmethod
        def container_running(_name):
            return True

        @staticmethod
        def copy_to(_container, _source, _destination):
            return None

        @staticmethod
        def exec(_container, _command, *, check=True, user=None):
            return 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    result = main(
        [
            "--state-root",
            str(store.root),
            "task",
            project.id,
            "uart",
            str(spec),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "project: silicon" in output
    assert f"project definition: {project_file}" in output
    assert "writable workspace mounts:" in output
    assert "source: /workspace/source" in output
    assert "read-only mounts:" in output
    assert "specifications: /readonly/specifications" in output
    assert "read-only inputs are below /readonly/<name>" in output

def _project_state_instance(
    identifier: str,
    team_path: Path,
    definition: Path,
) -> Instance:
    return Instance(
        id=identifier,
        team_name=team_path.name,
        team_path=str(team_path.resolve()),
        project_path=str(definition.parent.resolve()),
        generation="team-generation",
        providers=["codex"],
        models=["codex/model"],
        container_name=f"cyclo-{identifier}",
        network_name=f"cyclo-{identifier}-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=False,
        project_name="integration-project",
        project_file=str(definition.resolve()),
        project_description="Exercise multiple Cyclo teams and named mounts.",
        project_generation="project-generation",
        project_mounts=[
            {
                "name": "source",
                "path": str(definition.parent.resolve()),
                "mode": "rw",
            }
        ],
    )


def test_stop_project_file_stops_every_matching_configured_team(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project_definition(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    store = StateStore(tmp_path / "state")
    identifiers = (
        "integration-project-review-team",
        "integration-project-review-audit",
    )
    store.save(_project_state_instance(identifiers[0], team_repo, definition))
    store.save(_project_state_instance(identifiers[1], second_team, definition))
    stopped: list[str] = []
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda _args, selected_store, identifier: (
            selected_store.root == store.root or pytest.fail("wrong state store")
        )
        and stopped.append(identifier),
    )

    result = main(
        ["--state-root", str(store.root), "stop", str(definition)]
    )

    assert result == 0
    assert stopped == sorted(identifiers)
    output = capsys.readouterr().out
    for identifier in identifiers:
        assert f"stopped Cyclo instance: {identifier}" in output


def test_stop_project_file_continues_after_one_instance_fails(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project_definition(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    store = StateStore(tmp_path / "state")
    first = "integration-project-review-team"
    second = "integration-project-review-audit"
    store.save(_project_state_instance(first, team_repo, definition))
    store.save(_project_state_instance(second, second_team, definition))
    attempted: list[str] = []

    def stop(_args, _store, identifier):
        attempted.append(identifier)
        if identifier == first:
            raise CycloError("injected cleanup failure")

    monkeypatch.setattr("cyclo.cli.stop_instance", stop)

    result = main(
        ["--state-root", str(store.root), "stop", str(definition)]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert set(attempted) == {first, second}
    assert f"stopped Cyclo instance: {second}" in captured.out
    assert "stop incomplete" in captured.err
    assert "injected cleanup failure" in captured.err


@pytest.mark.parametrize("definition_state", ["invalid", "deleted"])
def test_stop_project_file_uses_persisted_bindings_after_definition_changes(
    definition_state: str,
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = write_project_definition(
        tmp_path / "project.cyclo", team_repo, project_repo
    )
    removed_team = tmp_path / "removed-team"
    shutil.copytree(team_repo, removed_team)
    store = StateStore(tmp_path / "state")
    first = _project_state_instance(
        "integration-project-review-team", team_repo, definition
    )
    removed = _project_state_instance(
        "integration-project-removed-team", removed_team, definition
    )
    store.save(first)
    store.save(removed)
    if definition_state == "invalid":
        definition.write_text("temporarily invalid\n", encoding="utf-8")
    else:
        definition.unlink()
    stopped: list[str] = []
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda _args, _store, identifier: stopped.append(identifier),
    )

    assert main(["--state-root", str(store.root), "stop", str(definition)]) == 0
    assert set(stopped) == {first.id, removed.id}


def test_project_binding_matches_through_a_symlinked_parent(
    tmp_path: Path, team_repo: Path, project_repo: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    definition_path = write_project_definition(
        real / "project.cyclo", team_repo, project_repo
    )
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    definition = load_project(alias / "project.cyclo")
    team = load_team(team_repo)
    previous = _project_state_instance(
        "integration-project-review-team", team_repo, definition_path
    )
    binding = RunBinding(
        team=team,
        project_root=definition.path.parent,
        instance=previous,
        manifest=render_project_manifest(definition, team=definition.teams[0]),
        project_mounts=definition.mounts,
    )

    assert _binding_matches(previous, binding)


def test_mount_source_identity_change_fails_before_container_start(
    tmp_path: Path, team_repo: Path, project_repo: Path
) -> None:
    team = load_team(team_repo)
    instance = _project_state_instance("identity-check", team_repo, tmp_path / "p.cyclo")
    binding = RunBinding(
        team=team,
        project_root=project_repo,
        instance=instance,
        manifest="",
        source_identities=_capture_source_identities((team.root, project_repo)),
    )
    moved = tmp_path / "moved-project"
    project_repo.rename(moved)
    project_repo.mkdir()

    with pytest.raises(CycloError, match="mount source changed after validation"):
        _verify_source_identities(binding)


def test_running_parent_mount_blocks_nested_source_substitution(
    tmp_path: Path, team_repo: Path, project_repo: Path
) -> None:
    store = StateStore(tmp_path / "state")
    active_project = tmp_path / "active-project"
    nested = active_project / "nested"
    active_team = tmp_path / "active-team"
    new_team_path = tmp_path / "new-team"
    for path in (nested, active_team, new_team_path):
        path.mkdir(parents=True)
    active = Instance(
        id="active",
        team_name="active-team",
        team_path=str(active_team),
        project_path=str(active_project),
        generation="generation",
        providers=[],
        models=[],
        container_name="cyclo-active",
        network_name="cyclo-active-net",
        image="cyclo-runtime:test",
        team_write=False,
        offline=False,
        active=True,
    )
    store.save(active)
    new_team = load_team(team_repo)
    binding = RunBinding(
        team=new_team,
        project_root=nested.resolve(),
        instance=_project_state_instance("new", team_repo, tmp_path / "new.cyclo"),
        manifest="",
        project_mounts=(ProjectMount("nested", nested.resolve(), "rw", 1),),
    )

    class RunningDocker:
        @staticmethod
        def container_lifecycle_active(name):
            return name == active.container_name

    with pytest.raises(CycloError, match="overlaps project of running instance"):
        _validate_running_mount_boundaries(binding, store, RunningDocker())


def test_running_instance_allows_exact_shared_project_mount(
    tmp_path: Path, team_repo: Path, project_repo: Path
) -> None:
    store = StateStore(tmp_path / "state")
    active_team = tmp_path / "active-team"
    active_team.mkdir()
    definition = tmp_path / "project.cyclo"
    active = _project_state_instance("active", active_team, definition)
    active.project_mounts = [
        {
            "name": "source",
            "path": str(project_repo.resolve()),
            "mode": "rw",
        }
    ]
    active.active = True
    store.save(active)
    team = load_team(team_repo)
    binding = RunBinding(
        team=team,
        project_root=definition.parent,
        instance=_project_state_instance("new", team_repo, definition),
        manifest="",
        project_mounts=(
            ProjectMount("source", project_repo.resolve(), "rw", 1),
        ),
    )

    class RunningDocker:
        @staticmethod
        def container_lifecycle_active(name):
            return name == active.container_name

    _validate_running_mount_boundaries(binding, store, RunningDocker())


def test_rollback_refuses_to_stop_a_replacement_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state")
    replacement = _project_state_instance(
        "replacement", tmp_path / "team", tmp_path / "project.cyclo"
    )
    replacement.launch_id = "new-launch"
    replacement.active = True
    store.save(replacement)
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: SimpleNamespace()
    )

    with pytest.raises(CycloError, match="replaced during project rollback"):
        stop_instance(
            SimpleNamespace(),
            store,
            replacement.id,
            expected_launch_id="old-launch",
        )

    assert store.load(replacement.id).active is True


class _StartupRuntime:
    container_name = "cyclo-provider-runtime-test"

    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events

    def rotate_client_token(self, identifier: str) -> None:
        self.events.append(("rotate", identifier))

    def prepare_instance(self, instance, _team, _running) -> None:
        self.events.append(("prepare", instance.id))

    def update_clients(self, running) -> None:
        self.events.append(("publish", tuple(item.id for item in running)))


class _StartupDocker:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        start_error: BaseException | None,
        stop_error: Exception | None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error

    @staticmethod
    def container_running(_name) -> bool:
        return False

    @staticmethod
    def container_lifecycle_state(_name) -> DockerContainerState:
        return DockerContainerState.STOPPED

    @staticmethod
    def container_lifecycle_active(_name) -> bool:
        return False

    def start(self, spec):
        self.events.append(("start", spec.instance.id))
        if self.start_error is None:
            pytest.fail("container start must not be reached")
        raise self.start_error

    @staticmethod
    def wait_ready(*_args, **_kwargs) -> None:
        pytest.fail("container readiness must not be reached")

    def stop_remove(self, container, instance, expected_launch=None) -> None:
        self.events.append(("stop", container, instance, expected_launch))
        if self.stop_error is not None:
            raise self.stop_error

    def remove_network(self, network, runtime) -> None:
        self.events.append(("remove-network", network, runtime))


@dataclass
class _StartupCase:
    store: StateStore
    binding: RunBinding
    runtime: _StartupRuntime
    docker: _StartupDocker
    events: list[tuple[object, ...]]

    def start(self) -> None:
        _start_binding(
            SimpleNamespace(port=0, verbose=False),
            self.binding,
            packaged_agentws_template().parent,
            self.store,
            self.runtime,
            self.docker,
            build=False,
        )

    def assert_rolled_back(self) -> None:
        instance = self.store.load(self.binding.instance.id)
        assert instance.active is False
        assert instance.port is None
        assert (
            "stop",
            instance.container_name,
            instance.id,
            instance.launch_id,
        ) in self.events
        assert (
            "remove-network",
            instance.network_name,
            self.runtime.container_name,
        ) in self.events

    @property
    def start_attempted(self) -> bool:
        return any(event[0] == "start" for event in self.events)


@pytest.fixture
def startup_case(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "cyclo.project_run.ensure_team_runtime_image", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "cyclo.project_run.attach_active_networks", lambda *_a, **_k: None
    )

    def create(
        identifier: str,
        launch_id: str,
        *,
        start_error: BaseException | None = None,
        stop_error: Exception | None = None,
        inspection_error: Exception | None = None,
        interrupt_after_save: bool = False,
    ) -> _StartupCase:
        store = StateStore(tmp_path / "state")
        team = load_team(team_repo)
        instance = Instance(
            id=identifier,
            team_name=team.name,
            team_path=str(team.root),
            project_path=str(project_repo),
            generation="generation",
            providers=list(team.providers),
            models=sorted({agent.model for agent in team.agents}),
            container_name=f"cyclo-{identifier}",
            network_name=f"cyclo-{identifier}-net",
            image="cyclo-runtime:test",
            team_write=False,
            offline=False,
            active=True,
            launch_id=launch_id,
        )
        binding = RunBinding(
            team=team,
            project_root=project_repo,
            instance=instance,
            manifest="# Project\n",
            source_identities=_capture_source_identities((team.root, project_repo)),
        )
        events: list[tuple[object, ...]] = []
        case = _StartupCase(
            store,
            binding,
            _StartupRuntime(events),
            _StartupDocker(
                events, start_error=start_error, stop_error=stop_error
            ),
            events,
        )

        if inspection_error is not None:
            def inspect(_store, _docker, *, candidate=None, stale=None):
                if candidate is not None:
                    raise inspection_error
                return []

            monkeypatch.setattr("cyclo.project_run.active_instances", inspect)

        if interrupt_after_save:
            real_save = store.save
            save_calls = 0

            def interrupting_save(selected: Instance) -> None:
                nonlocal save_calls
                save_calls += 1
                real_save(selected)
                events.append(("save", selected.active))
                if save_calls == 1:
                    raise KeyboardInterrupt

            monkeypatch.setattr(store, "save", interrupting_save)

        return case

    return create


def test_failed_start_cleanup_is_launch_pinned_and_reports_failure(
    startup_case,
) -> None:
    case = startup_case(
        "failed-start",
        "original-launch",
        start_error=CycloError("injected start failure"),
        stop_error=CycloError("replacement launch refused"),
    )

    with pytest.raises(CycloError, match="rollback incomplete") as stopped:
        case.start()

    assert "replacement launch refused" in str(stopped.value)
    assert case.start_attempted
    case.assert_rolled_back()


def test_startup_state_inspection_failure_rolls_back_persisted_instance(
    startup_case,
) -> None:
    case = startup_case(
        "inspection-failure",
        "inspection-launch",
        inspection_error=RuntimeError("injected state inspection failure"),
    )

    with pytest.raises(RuntimeError, match="injected state inspection failure"):
        case.start()

    assert not case.start_attempted
    case.assert_rolled_back()


def test_interrupt_after_initial_state_commit_still_rolls_back(
    startup_case,
) -> None:
    case = startup_case(
        "save-boundary-interrupt",
        "save-boundary-launch",
        interrupt_after_save=True,
    )

    with pytest.raises(KeyboardInterrupt):
        case.start()

    assert ("save", True) in case.events
    assert not case.start_attempted
    case.assert_rolled_back()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_keyboard_interrupt_during_startup_rolls_back_current_instance(
    cleanup_failure: bool,
    startup_case,
) -> None:
    case = startup_case(
        "interrupted-start",
        "interrupted-launch",
        start_error=KeyboardInterrupt(),
        stop_error=(
            CycloError("injected interrupted cleanup failure")
            if cleanup_failure
            else None
        ),
    )

    expected_error = CycloError if cleanup_failure else KeyboardInterrupt
    with pytest.raises(expected_error) as stopped:
        case.start()

    assert case.start_attempted
    case.assert_rolled_back()
    if cleanup_failure:
        assert "KeyboardInterrupt" in str(stopped.value)


def test_project_startup_interrupt_rolls_back_earlier_teams(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    second_team = tmp_path / "review-audit"
    shutil.copytree(team_repo, second_team)
    definition = write_project_definition(
        tmp_path / "project.cyclo",
        team_repo,
        project_repo,
        second_team=second_team,
    )
    started: list[str] = []
    stopped: list[tuple[str, str | None]] = []

    class FakeRuntime:
        @staticmethod
        def require_running():
            return None

    monkeypatch.setattr("cyclo.cli.gateway", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda *_args: FakeRuntime()
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli._validate_project_mounts", lambda *_args: None)
    monkeypatch.setattr("cyclo.cli.preflight_binding", lambda *_args: None)

    def start(_args, binding, _source, store, *_positional, **_kwargs):
        if started:
            store.save(binding.instance)
            raise KeyboardInterrupt
        started.append(binding.instance.id)

    def stop(_args, _store, identifier, *, expected_launch_id=None):
        stopped.append((identifier, expected_launch_id))

    monkeypatch.setattr("cyclo.cli.start_binding", start)
    monkeypatch.setattr("cyclo.cli.stop_instance", stop)

    result = main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "run",
            str(definition),
        ]
    )

    assert result == 130
    assert len(started) == 1
    assert [identifier for identifier, _launch in stopped] == [
        "integration-project-review-audit",
        started[0],
    ]
    assert all(launch for _identifier, launch in stopped)
    assert "interrupted" in capsys.readouterr().err


def test_project_startup_warns_when_inflight_launch_state_is_unreadable(
    tmp_path: Path,
    team_repo: Path,
    project_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    definition = write_project_definition(
        tmp_path / "project.cyclo", team_repo, project_repo
    )

    class FakeRuntime:
        @staticmethod
        def require_running():
            return None

    monkeypatch.setattr("cyclo.cli.gateway", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda *_args: FakeRuntime()
    )
    monkeypatch.setattr("cyclo.cli.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.cli._validate_project_mounts", lambda *_args: None)
    monkeypatch.setattr("cyclo.cli.preflight_binding", lambda *_args: None)

    def start(_args, binding, _source, store, *_positional, **_kwargs):
        metadata = store.metadata_path(binding.instance.id)
        metadata.parent.mkdir(parents=True)
        metadata.write_text("{invalid\n", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr("cyclo.cli.start_binding", start)
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda *_args, **_kwargs: pytest.fail(
            "an unreadable launch identity must not be stopped blindly"
        ),
    )

    result = main(
        [
            "--state-root",
            str(tmp_path / "state"),
            "run",
            str(definition),
        ]
    )

    assert result == 130
    error = capsys.readouterr().err
    assert "project startup rollback was incomplete" in error
    assert "cannot verify the in-flight launch" in error
    assert "interrupted" in error


def test_stop_prefers_an_existing_legacy_instance_id_over_project_like_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    identifier = "project.cyclo"
    store.save(
        Instance(
            id=identifier,
            team_name="legacy-team",
            team_path="/teams/legacy",
            project_path="/projects/legacy",
            generation="generation",
            providers=["codex"],
            models=["codex/model"],
            container_name=f"cyclo-{identifier}",
            network_name=f"cyclo-{identifier}-net",
            image="cyclo-runtime:test",
            team_write=False,
            offline=False,
        )
    )
    (tmp_path / identifier).write_text("not a valid project\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    stopped: list[str] = []
    monkeypatch.setattr(
        "cyclo.cli.stop_instance",
        lambda _args, _store, selected: stopped.append(selected),
    )

    result = main(["--state-root", str(store.root), "stop", identifier])

    assert result == 0
    assert stopped == [identifier]
    assert f"stopped Cyclo instance: {identifier}" in capsys.readouterr().out


def test_ps_uses_logical_project_name_for_project_file_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    definition = tmp_path / "project.cyclo"
    project = _project_state_instance(
        "integration-project-review-team",
        tmp_path / "review-team",
        definition,
    )
    store.save(project)

    class FakeDocker:
        @staticmethod
        def container_running(_name):
            return False

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["--state-root", str(store.root), "ps"]) == 0

    output = capsys.readouterr().out
    assert "PROJECT" in output
    assert "integration-project" in output
    assert definition.parent.name not in output.splitlines()[-1]


def test_ps_reports_running_team_as_runtime_down_when_runtime_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _project_state_instance(
        "integration-project-review-team",
        tmp_path / "review-team",
        tmp_path / "project.cyclo",
    )
    selected.active = True
    store.save(selected)
    queue_root = store.queue_root(selected.id)
    for name in ("tasks", "jobs", "agents"):
        (queue_root / name).mkdir(parents=True, exist_ok=True)
    runs = queue_root / "agents" / ".team-runs"
    runs.mkdir()
    (runs / "supervisor.ready").write_text("pid=123\n", encoding="utf-8")

    class FakeDocker:
        @staticmethod
        def container_running(_name):
            return True

    class FakeRuntime:
        calls = 0

        @classmethod
        def status(cls):
            cls.calls += 1
            return SimpleNamespace(exists=True, running=False, current=True)

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )

    assert main(["--state-root", str(store.root), "ps"]) == 0

    output = capsys.readouterr().out
    assert "HEALTH" in output
    assert "running" in output
    assert "runtime-down (runtime container stopped)" in output
    assert FakeRuntime.calls == 1


def test_ps_refuses_to_report_an_incomplete_instance_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    store.save(
        _project_state_instance(
            "valid-instance",
            tmp_path / "team",
            tmp_path / "project.cyclo",
        )
    )
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    payload = _project_state_instance(
        "broken", tmp_path / "broken-team", tmp_path / "broken.cyclo"
    ).as_json()
    payload["project_path"] = {"not": "a path"}
    broken.write_text(json.dumps(payload), encoding="utf-8")

    class FakeDocker:
        @staticmethod
        def container_running(_name):
            raise AssertionError("ps must reject incomplete state before Docker inspection")

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)

    assert main(["--state-root", str(store.root), "ps"]) == 1

    error = capsys.readouterr().err
    assert "cannot enumerate Cyclo instance state" in error
    assert str(broken) in error


def test_ps_reports_agentws_supervisor_suspension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    selected = _project_state_instance(
        "integration-project-review-team",
        tmp_path / "review-team",
        tmp_path / "project.cyclo",
    )
    selected.active = True
    store.save(selected)
    runs = store.agents_dir(selected.id) / ".team-runs"
    runs.mkdir(parents=True)
    store.tasks_dir(selected.id).mkdir(parents=True)
    store.jobs_dir(selected.id).mkdir(parents=True)
    (runs / "supervisor.ready").write_text("pid=123\n", encoding="utf-8")
    (runs / "planner-1.suspended").write_text(
        "reason=fatal-agent-safety-error\n", encoding="utf-8"
    )

    class FakeDocker:
        @staticmethod
        def container_running(_name):
            return True

    class FakeRuntime:
        @staticmethod
        def status():
            return SimpleNamespace(exists=True, running=True, current=True)

        @staticmethod
        def probe_operational(*, timeout):
            assert timeout > 0

    monkeypatch.setattr("cyclo.cli.Docker", FakeDocker)
    monkeypatch.setattr(
        "cyclo.cli.provider_service", lambda _args, _store: FakeRuntime()
    )

    assert main(["--state-root", str(store.root), "ps"]) == 0

    output = capsys.readouterr().out
    assert "running" in output
    assert "agents-suspended (1 agent suspended: planner-1)" in output


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
        ]
    ]


def test_gateway_status_rejects_build(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["gateway", "status", "--build"])

    assert stopped.value.code == 2
    assert "unrecognized arguments: --build" in capsys.readouterr().err


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
        "cyclo.instance_lifecycle.active_instances",
        lambda _store, _docker, *, stale: [remaining],
    )
    def attach(_docker, _runtime, instances):
        events.append(("attach", tuple(item.id for item in instances)))
        if network_failure:
            raise CycloError("injected network drift")

    monkeypatch.setattr("cyclo.instance_lifecycle.attach_active_networks", attach)

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
    monkeypatch.setattr("cyclo.provider_commands.HostProviders", FakeHost)
    monkeypatch.setattr("cyclo.cli.provider_service", lambda _args, _store: FakeService())
    monkeypatch.setattr(
        "cyclo.cli.host_configuration",
        lambda _args: SimpleNamespace(load=lambda: (definition,)),
    )
    monkeypatch.setattr("cyclo.provider_commands.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.provider_commands.active_instances", lambda _store, _docker: [])
    monkeypatch.setattr("cyclo.provider_commands.time.time_ns", lambda: 1_234_567_890_000)
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
    monkeypatch.setattr("cyclo.provider_commands.HostProviders", FakeHost)
    monkeypatch.setattr("cyclo.cli.provider_service", lambda _args, _store: FakeService())
    monkeypatch.setattr(
        "cyclo.cli.host_configuration",
        lambda _args: SimpleNamespace(load=lambda: (definition,)),
    )
    monkeypatch.setattr("cyclo.provider_commands.Docker", lambda: SimpleNamespace())
    monkeypatch.setattr("cyclo.provider_commands.active_instances", lambda _store, _docker: [])

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

    monkeypatch.setattr("cyclo.provider_commands.ProviderRuntime", FakeProviderRuntime)

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

        def probe_operational(self, *, timeout):
            assert timeout > 0

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


def test_doctor_fails_when_gateway_is_unavailable_behind_a_current_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"

    class FakeDocker:
        @staticmethod
        def available():
            return True, "test daemon"

    class FakeService:
        state_root = state / "provider-runtime"
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def status():
            return SimpleNamespace(running=True, current=True)

        @staticmethod
        def probe_operational(*, timeout):
            assert timeout > 0
            raise CycloError("credential gateway unavailable: connection refused")

        @staticmethod
        def catalog():
            raise AssertionError("doctor must not trust a cached catalog after probe failure")

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

    assert main(
        [
            "--state-root",
            str(state),
            "--host-config",
            str(tmp_path / "missing-host.conf"),
            "doctor",
        ]
    ) == 1

    output = capsys.readouterr().out
    assert (
        "no  provider runtime: credential gateway unavailable: connection refused"
        in output
    )
    assert "ok  provider runtime catalog" not in output


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            SimpleNamespace(exists=False, running=False, current=False),
            "provider runtime is not running; run `cyclo runtime start`",
        ),
        (
            SimpleNamespace(exists=True, running=False, current=True),
            "provider runtime is not running; run `cyclo runtime restart`",
        ),
        (
            SimpleNamespace(exists=True, running=True, current=False),
            "provider runtime is stale; run `cyclo runtime restart`",
        ),
    ],
)
def test_doctor_prescribes_the_valid_runtime_lifecycle_action(
    status: SimpleNamespace,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"

    class FakeDocker:
        @staticmethod
        def available():
            return True, "test daemon"

    class FakeService:
        state_root = state / "provider-runtime"
        container_name = "cyclo-provider-runtime-test"

        @staticmethod
        def status():
            return status

        @staticmethod
        def probe_operational(*, timeout):
            raise AssertionError("an absent, stopped, or stale runtime must not be probed")

        @staticmethod
        def catalog():
            raise AssertionError("an absent, stopped, or stale runtime has no live catalog")

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

    assert main(
        [
            "--state-root",
            str(state),
            "--host-config",
            str(tmp_path / "missing-host.conf"),
            "doctor",
        ]
    ) == 1

    output = capsys.readouterr().out
    assert f"no  provider runtime: {message}" in output
    assert "provider runtime catalog" not in output


def test_doctor_reports_corrupt_persisted_instance_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "state"
    store = StateStore(state)
    broken = store.metadata_path("broken")
    broken.parent.mkdir(parents=True)
    payload = _project_state_instance(
        "broken", tmp_path / "broken-team", tmp_path / "broken.cyclo"
    ).as_json()
    payload["project_mounts"] = None
    broken.write_text(json.dumps(payload), encoding="utf-8")

    class UnavailableDocker:
        @staticmethod
        def available():
            return False, "test daemon unavailable"

    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: tmp_path / "agentws")
    monkeypatch.setattr(
        "cyclo.cli.gateway",
        lambda _args, _store: SimpleNamespace(
            gateway=SimpleNamespace(__file__="credential-gateway.py")
        ),
    )
    monkeypatch.setattr("cyclo.cli.Docker", UnavailableDocker)

    assert main(
        [
            "--state-root",
            str(state),
            "--host-config",
            str(tmp_path / "missing-host.conf"),
            "doctor",
        ]
    ) == 1

    output = capsys.readouterr().out
    assert "no  persisted instance state:" in output
    assert str(broken) in output


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
        lambda arguments, **_kwargs: events.append(tuple(arguments)) or 0,
    )

    assert main(["gateway", "login", "openai", "--api-key-stdin"]) == 0

    assert isinstance(events[0], tuple)
    assert events[1:] == (["status", "reload"] if running else ["status"])


def test_gateway_login_guard_uses_command_local_image_and_store_before_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state")
    events: list[object] = []

    class FakeGateway:
        @staticmethod
        def validate_login() -> None:
            events.append("validated")

    def selected_gateway(args, selected_store):
        assert selected_store is store
        events.append(("gateway", args.gateway_image, args.store_volume))
        return FakeGateway()

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.gateway", selected_gateway)
    monkeypatch.setattr(
        "cyclo.credential_gateway.gateway.ensure_gateway_image",
        lambda image, *, build=False: events.append(("image", image, build)),
    )
    monkeypatch.setattr(
        "cyclo.credential_gateway.cli.docker.run_command",
        lambda command: events.append(("login", command)) or 0,
    )
    monkeypatch.setattr(
        "cyclo.cli._reload_runtime_after_gateway_change",
        lambda selected_args, _store: events.append(
            ("reload", selected_args.gateway_image, selected_args.store_volume)
        ),
    )

    assert (
        main(
            [
                "--gateway-image",
                "outer-image",
                "--store-volume",
                "outer-store",
                "gateway",
                "login",
                "--image",
                "selected-image",
                "--store-volume",
                "selected-store",
                "openai",
                "--api-key-stdin",
            ]
        )
        == 0
    )

    assert events[:3] == [
        ("image", "selected-image", False),
        ("gateway", "selected-image", "selected-store"),
        "validated",
    ]
    login_event = events[3]
    assert isinstance(login_event, tuple)
    assert login_event[0] == "login"
    command = login_event[1]
    assert isinstance(command, list)
    assert "selected-image" in command
    assert (
        "type=volume,src=selected-store,dst=/var/lib/cyclo-gateway" in command
    )
    assert "outer-image" not in command
    assert not any("outer-store" in argument for argument in command)
    assert events[4:] == [("reload", "selected-image", "selected-store")]


def test_gateway_login_refuses_before_store_mount_when_guard_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = StateStore(tmp_path / "state")
    commands: list[list[str]] = []

    class StaleGateway:
        @staticmethod
        def validate_login() -> None:
            raise CycloError(
                "running gateway is stale; run `cyclo gateway restart --build`"
            )

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.gateway", lambda _args, _store: StaleGateway()
    )
    monkeypatch.setattr(
        "cyclo.credential_gateway.gateway.ensure_gateway_image",
        lambda _image, *, build=False: None,
    )
    monkeypatch.setattr(
        "cyclo.credential_gateway.cli.docker.run_command",
        lambda command: commands.append(command) or 0,
    )
    monkeypatch.setattr(
        "cyclo.cli._reload_runtime_after_gateway_change",
        lambda *_args: pytest.fail("failed login must not refresh the runtime"),
    )

    assert main(["gateway", "login", "openai", "--api-key-stdin"]) == 1

    assert commands == []
    assert "cyclo gateway restart --build" in capsys.readouterr().err


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

    monkeypatch.setattr("cyclo.provider_commands.HostProviders", FakeHost)
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
