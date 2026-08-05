from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.cli import (
    _gateway_login_arguments,
    _normalize_global_options,
    _running_instance_condition,
    build_parser,
    cmd_component,
    cmd_doctor,
    cmd_gateway,
    cmd_models,
    cmd_ps,
    cmd_refresh,
    cmd_run,
    cmd_stop,
    cmd_task_add_job,
    cmd_task_run,
    main,
    state_store,
)
from cyclo.dcomp import DCompComponentStatus, DCompStatus
from cyclo.errors import CycloError


def namespace(**values):
    defaults = {"state_root": None}
    defaults.update(values)
    return SimpleNamespace(**defaults)


class MemoryStore:
    def __init__(self, instances=()):
        self.instances = {item.id: item for item in instances}
        self.saved: list[str] = []
        self.system = "test"

    def locked(self, **_options):
        return nullcontext()

    def list(self):
        return list(self.instances.values())

    def list_report(self):
        return self.list(), []

    def load(self, identifier):
        try:
            return self.instances[identifier]
        except KeyError as exc:
            raise CycloError(f"Cyclo instance not found: {identifier}") from exc

    def save(self, instance):
        self.instances[instance.id] = instance
        self.saved.append(instance.id)

    def save_many(self, instances):
        for instance in instances:
            self.save(instance)


def dcomp_status(*components: DCompComponentStatus) -> DCompStatus:
    return DCompStatus(
        api_version=1,
        name="cyclo-test",
        desired=True,
        operational=all(
            item.status == "running" and item.health == "healthy"
            for item in components
        ),
        digest="a" * 64,
        operation="",
        phase="",
        networks=(),
        components=components,
    )


def component(name: str) -> DCompComponentStatus:
    return DCompComponentStatus(
        name=name,
        container_id="a" * 64,
        status="running",
        health="healthy",
        exit_code=0,
        problem="",
        published_ports=(),
    )


def test_ps_prints_headers_for_an_empty_installation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()

    class Runtime:
        host = SimpleNamespace()

        @staticmethod
        def status():
            return dcomp_status()

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: Runtime())

    assert cmd_ps(namespace()) == 0
    assert capsys.readouterr().out.splitlines() == [
        "INSTANCE  TEAM  PROJECT  DESIRED  STATUS  PORT"
    ]


def test_gateway_status_reports_unavailable_credential_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()

    class DComp:
        @staticmethod
        def volume(_system: str, _component: str, _logical_name: str) -> str:
            raise CycloError("dcomp volume failed with status 1: volume is absent")

    class Runtime:
        name = "cyclo-test"
        dcomp = DComp()

        @staticmethod
        def status():
            return dcomp_status(component("gateway"))

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: Runtime())

    assert cmd_gateway(namespace(gateway_action="status")) == 1
    output = capsys.readouterr().out
    assert "gateway" in output
    assert "credential store: unavailable (dcomp volume failed" in output


def test_cli_surface_has_declarative_provider_and_component_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["providers", "check"]).providers_action == "check"
    assert parser.parse_args(["providers", "status"]).providers_action == "status"
    assert parser.parse_args(["providers", "restart"]).providers_action == "restart"
    assert parser.parse_args(["component", "list"]).component_action == "list"
    assert parser.parse_args(
        ["component", "logs", "-f", "gateway"]
    ).follow
    add_job = parser.parse_args(
        [
            "task",
            "add-job",
            "demo",
            "pcie",
            "pcie-rtl-r4",
            "rtl",
            "recovery.md",
        ]
    )
    assert add_job.task_action == "add-job"
    assert add_job.role == "rtl"

    with pytest.raises(SystemExit):
        parser.parse_args(["providers", "start"])
    with pytest.raises(SystemExit):
        parser.parse_args(["component", "stop", "gateway"])


def test_global_state_root_is_accepted_after_subcommands(tmp_path: Path) -> None:
    arguments = _normalize_global_options(
        ["task", "list", "demo", "--state-root", str(tmp_path)]
    )

    assert arguments[:2] == ["--state-root", str(tmp_path)]
    assert arguments[2:] == ["task", "list", "demo"]


def test_explicit_state_root_selects_local_host_configuration(
    tmp_path: Path,
) -> None:
    store = state_store(namespace(state_root=str(tmp_path)))

    assert store.root == tmp_path
    assert store.host_config_scope == "local"


def test_run_persists_intent_before_final_dcomp_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    instance = SimpleNamespace(id="demo-team")
    binding = SimpleNamespace(instance=instance, team=SimpleNamespace())
    store = MemoryStore()
    status = dcomp_status(component("gateway"), component("team-demo"))

    class Runtime:
        def validate_project_mounts(self, definition, teams):
            events.append("validate-mounts")

        def apply(self, instances):
            events.append("apply:" + ",".join(item.id for item in instances))
            return SimpleNamespace(status=status)

        def validate_instances(self, instances):
            events.append(
                "validate-instances:"
                + ",".join(item.id for item in instances)
            )

        def require_instances_ready(self, instances, _status):
            events.append(
                "require-ready:" + ",".join(item.id for item in instances)
            )

    runtime = Runtime()
    monkeypatch.setattr("cyclo.cli.load_project", lambda _path: SimpleNamespace())
    monkeypatch.setattr(
        "cyclo.cli.load_project_teams",
        lambda _definition: ((SimpleNamespace(), SimpleNamespace()),),
    )
    monkeypatch.setattr("cyclo.cli.validate_run_options", lambda *_a, **_kw: None)
    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: Path("/agentws"))
    monkeypatch.setattr(
        "cyclo.cli.project_instance_id",
        lambda _definition, _selected: "demo-team",
    )
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: runtime)
    monkeypatch.setattr("cyclo.cli._catalogue", lambda *_args: {"models": []})
    monkeypatch.setattr(
        "cyclo.cli.validate_pi_team_models",
        lambda *_args: events.append("validate-models"),
    )
    monkeypatch.setattr(
        "cyclo.cli._prepare_run_bindings",
        lambda *_args: (binding,),
    )
    monkeypatch.setattr(
        "cyclo.cli.verify_source_identities",
        lambda _binding: events.append("verify-mounts"),
    )
    monkeypatch.setattr(
        "cyclo.cli._announce_instance",
        lambda *_args: events.append("announce"),
    )
    original_save_many = store.save_many

    def save_many(selected):
        cohort = tuple(selected)
        events.append("save-many:" + ",".join(item.id for item in cohort))
        original_save_many(cohort)

    store.save_many = save_many

    assert cmd_run(
        namespace(
            project="project.cyclo",
            image=None,
            offline=False,
            verbose=False,
            host="127.0.0.1",
            port=0,
            foreground=False,
        )
    ) == 0
    assert events == [
        "validate-mounts",
        "apply:",
        "validate-models",
        "verify-mounts",
        "validate-instances:demo-team",
        "save-many:demo-team",
        "apply:demo-team",
        "require-ready:demo-team",
        "announce",
    ]


def test_stop_persists_stopped_intent_before_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    instance = SimpleNamespace(id="demo", intent="running")
    store = MemoryStore((instance,))

    class Runtime:
        def apply(self, instances):
            events.append(
                "apply:" + ",".join(f"{item.id}={item.intent}" for item in instances)
            )

    runtime = Runtime()
    original_save = store.save

    def save(selected):
        events.append(f"save:{selected.id}={selected.intent}")
        original_save(selected)

    store.save = save
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: runtime)

    assert cmd_stop(namespace(target="demo")) == 0
    assert events == ["save:demo=stopped", "apply:demo=stopped"]


def test_refresh_reconciles_provider_system_before_replacement_teams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stale = SimpleNamespace(id="demo", intent="running")
    replacement = SimpleNamespace(id="demo", intent="running")
    team = SimpleNamespace()
    store = MemoryStore((stale,))
    status = dcomp_status(component("gateway"))

    class Runtime:
        def validate_instances(self, instances):
            events.append(
                "validate:" + ",".join(item.id for item in instances)
            )

        def apply(self, instances, *, rebuild_host=False):
            selected = tuple(instances)
            events.append(
                "apply:"
                + ",".join(item.id for item in selected)
                + f":rebuild={str(rebuild_host).lower()}"
            )
            return SimpleNamespace(status=status)

        def require_instances_ready(self, instances, _status):
            events.append(
                "require-ready:" + ",".join(item.id for item in instances)
            )

    monkeypatch.setattr("cyclo.cli.agentws_root", lambda: Path("/agentws"))
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.cyclo_runtime",
        lambda _args, _store: Runtime(),
    )
    monkeypatch.setattr(
        "cyclo.cli._refresh_instance",
        lambda _runtime, _instance: (replacement, team),
    )
    monkeypatch.setattr("cyclo.cli._catalogue", lambda *_args: {"models": []})
    monkeypatch.setattr(
        "cyclo.cli.validate_pi_team_models",
        lambda *_args: events.append("validate-model"),
    )

    assert cmd_refresh(namespace()) == 0
    assert events == [
        "validate:demo",
        "apply::rebuild=true",
        "validate-model",
        "apply:demo:rebuild=false",
        "require-ready:demo",
    ]
    assert store.instances == {"demo": replacement}


def test_models_automatically_applies_system_before_catalogue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    store = MemoryStore()
    status = dcomp_status(component("gateway"))

    class Runtime:
        def apply(self, _instances):
            events.append("apply")
            return SimpleNamespace(status=status)

    runtime = Runtime()
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: runtime)
    monkeypatch.setattr(
        "cyclo.cli._catalogue",
        lambda selected, observed: (
            events.append("catalogue") or {"models": [{"id": "b"}, {"id": "a"}]}
        ),
    )

    assert cmd_models(namespace()) == 0
    assert events == ["apply", "catalogue"]
    assert capsys.readouterr().out == "a\nb\n"


def test_task_run_uses_confined_admin_tool_without_dcomp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    tasks = root / "tasks"
    jobs = root / "jobs"
    tasks.mkdir(parents=True)
    jobs.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("# Build it\n", encoding="utf-8")
    instance = SimpleNamespace(id="demo", image="sha256:" + "a" * 64)
    observed: list[tuple[str, tuple[str, ...], bytes | None]] = []

    class Store(MemoryStore):
        def queue_root(self, _identifier):
            return root

        def tasks_dir(self, _identifier):
            return tasks

        def jobs_dir(self, _identifier):
            return jobs

    store = Store((instance,))

    class Admin:
        def __init__(self, selected_store, selected_instance):
            assert selected_store is store
            assert selected_instance is instance

        def run(self, tool, arguments=(), *, specification=None):
            observed.append((tool, tuple(arguments), specification))
            return 0

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.TaskAdmin", Admin)
    monkeypatch.setattr(
        "cyclo.cli.cyclo_runtime",
        lambda *_args: pytest.fail("task operations must not require DComp"),
    )
    monkeypatch.setattr(
        "cyclo.cli._task_project_summary",
        lambda _instance: (),
    )

    assert cmd_task_run(
        namespace(instance="demo", task_id="uart", spec=str(spec))
    ) == 0
    assert observed == [
        ("task-create", ("uart",), b"# Build it\n"),
    ]


def test_task_add_job_uses_confined_admin_tool_without_dcomp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    tasks = root / "tasks"
    jobs = root / "jobs"
    tasks.mkdir(parents=True)
    jobs.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("# Repair the link\n", encoding="utf-8")
    instance = SimpleNamespace(id="demo", image="sha256:" + "a" * 64)
    observed: list[tuple[str, tuple[str, ...], bytes | None]] = []

    class Store(MemoryStore):
        def queue_root(self, _identifier):
            return root

        def tasks_dir(self, _identifier):
            return tasks

        def jobs_dir(self, _identifier):
            return jobs

    store = Store((instance,))

    class Admin:
        def __init__(self, selected_store, selected_instance):
            assert selected_store is store
            assert selected_instance is instance

        def run(self, tool, arguments=(), *, specification=None):
            observed.append((tool, tuple(arguments), specification))
            return 0

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.TaskAdmin", Admin)
    monkeypatch.setattr(
        "cyclo.cli.cyclo_runtime",
        lambda *_args: pytest.fail("task operations must not require DComp"),
    )

    assert cmd_task_add_job(
        namespace(
            instance="demo",
            task_id="pcie",
            job_id="pcie-rtl-r4",
            role="rtl",
            spec=str(spec),
        )
    ) == 0
    assert observed == [
        (
            "job-create",
            (
                "pcie-rtl-r4",
                "--role",
                "rtl",
                "--task-id",
                "pcie",
            ),
            b"# Repair the link\n",
        ),
    ]


def test_component_status_reports_configured_component_as_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    status = dcomp_status()
    runtime = SimpleNamespace(
        status=lambda: status,
        host=SimpleNamespace(providers=()),
        component_for_instance=lambda identifier: f"team-{identifier}",
    )
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: runtime)

    assert cmd_component(
        namespace(component_action="status", name="gateway")
    ) == 0
    output = capsys.readouterr().out
    assert "gateway" in output
    assert "absent" in output


def test_raw_component_diagnostics_do_not_require_host_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    observed = dcomp_status(component("gateway"), component("fusion"))
    log_calls: list[tuple[str, str, bool]] = []
    restart_calls: list[tuple[str, str]] = []

    class Runtime:
        name = "cyclo-test"
        dcomp = SimpleNamespace(
            logs=lambda system, name, *, follow, output: log_calls.append(
                (system, name, follow)
            ),
            restart=lambda system, name: restart_calls.append((system, name)),
        )

        @property
        def host(self):
            raise CycloError("host.conf:1: unknown directive 'invalid'")

        def status(self):
            return observed

        @staticmethod
        def component_for_instance(identifier):
            return f"team-{identifier}"

    runtime = Runtime()
    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr("cyclo.cli.cyclo_runtime", lambda _args, _store: runtime)

    assert cmd_component(namespace(component_action="list", name=None)) == 0
    assert (
        cmd_component(namespace(component_action="status", name="fusion"))
        == 0
    )
    assert (
        cmd_component(
            namespace(component_action="logs", name="fusion", follow=True)
        )
        == 0
    )
    assert (
        cmd_component(namespace(component_action="restart", name="fusion"))
        == 0
    )
    output = capsys.readouterr().out
    assert "gateway" in output
    assert "fusion" in output
    assert log_calls == [("cyclo-test", "fusion", True)]
    assert restart_calls == [("cyclo-test", "fusion")]


def test_ps_reports_applied_team_state_when_host_configuration_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = SimpleNamespace(
        id="demo",
        team_name="jon-rtl",
        project_name="core-et",
        intent="running",
        offline=True,
    )
    store = MemoryStore((selected,))
    observed = dcomp_status(component("gateway"), component("team-demo"))

    class Runtime:
        @property
        def host(self):
            raise CycloError("host.conf:1: unknown directive 'invalid'")

        def status(self):
            return observed

        @staticmethod
        def component_for_instance(identifier):
            return f"team-{identifier}"

        @staticmethod
        def team_port(_instance, _status):
            return None

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.cyclo_runtime",
        lambda _args, _store: Runtime(),
    )

    assert cmd_ps(namespace()) == 1
    captured = capsys.readouterr()
    assert "demo" in captured.out
    assert "ready" in captured.out
    assert "host configuration unavailable" in captured.err


def test_doctor_checks_applied_components_when_host_configuration_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    observed = dcomp_status(component("gateway"), component("fusion"))

    class Runtime:
        dcomp = SimpleNamespace(
            version=lambda: SimpleNamespace(version="0.1.0", api_version=1)
        )

        @property
        def host(self):
            raise CycloError("host.conf:1: unknown directive 'invalid'")

        def status(self):
            return observed

        @staticmethod
        def component_for_instance(identifier):
            return f"team-{identifier}"

    monkeypatch.setattr("cyclo.cli.state_store", lambda _args: store)
    monkeypatch.setattr(
        "cyclo.cli.cyclo_runtime",
        lambda _args, _store: Runtime(),
    )

    assert cmd_doctor(namespace()) == 1
    output = capsys.readouterr().out
    assert "ok  dcomp 0.1.0 (API 1)" in output
    assert "ok  component gateway" in output
    assert "ok  component fusion" in output
    assert "no  host configuration unavailable" in output


def test_running_instance_requires_its_outer_provider() -> None:
    team = component("team-demo")
    gateway = DCompComponentStatus(
        name="gateway",
        container_id="b" * 64,
        status="exited",
        health="unhealthy",
        exit_code=1,
        problem="provider failed",
        published_ports=(),
    )
    status = dcomp_status(gateway, team)
    runtime = SimpleNamespace(
        host=SimpleNamespace(outer_component="gateway"),
    )

    condition, detail = _running_instance_condition(runtime, status, team)

    assert condition == "not-ready"
    assert "provider failed" in detail


def test_gateway_login_arguments_do_not_expand_secret_environment() -> None:
    args = namespace(
        provider="openai",
        account="work",
        api_key_env="OPENAI_SECRET",
        api_key_stdin=False,
    )

    assert _gateway_login_arguments(args) == [
        "openai",
        "--as",
        "work",
        "--api-key-env",
        "OPENAI_SECRET",
    ]


def test_main_prints_one_user_facing_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args):
        raise CycloError("broken")

    monkeypatch.setattr("cyclo.cli.cmd_ps", fail)

    assert main(["ps"]) == 1
    assert capsys.readouterr().err == "error: broken\n"
