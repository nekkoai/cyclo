from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.dcomp import DCompComponentStatus, DCompStatus
from cyclo.dcomp_system import PROVIDER_SERVICE
from cyclo.errors import CycloError
from cyclo.runtime import CycloRuntime
from cyclo.state import Instance, StateStore
from cyclo.team.component import (
    PI_SETTINGS_TEMPLATE,
    make_team_component,
    materialize_instance,
)
from cyclo.team.image import require_non_root_team_host


class NoDComp:
    pass


class NoImages:
    endpoint = None


class AppliedDComp:
    executable = "/usr/bin/dcomp"

    def __init__(self, observed: DCompStatus) -> None:
        self.observed = observed
        self.calls: list[str] = []

    def check(self, _path: Path) -> None:
        self.calls.append("check")

    def status(self, _name: str) -> DCompStatus:
        self.calls.append("status")
        return self.observed

    def up(self, _path: Path) -> None:
        self.calls.append("up")

    def down(self, name: str) -> None:
        self.calls.append(f"down:{name}")


def runtime(tmp_path: Path, host_text: str = "") -> CycloRuntime:
    config = tmp_path / "host.conf"
    config.write_text(host_text, encoding="utf-8")
    return CycloRuntime(
        StateStore(tmp_path / "state"),
        config,
        dcomp=NoDComp(),  # type: ignore[arg-type]
        images=NoImages(),  # type: ignore[arg-type]
    )


def image(identifier: str):
    return SimpleNamespace(id="sha256:" + identifier * 64)


def instance(tmp_path: Path, identifier: str = "demo") -> Instance:
    team = tmp_path / "team"
    source = tmp_path / "source"
    team.mkdir(exist_ok=True)
    source.mkdir(exist_ok=True)
    return Instance(
        id=identifier,
        team_name="team",
        team_path=str(team),
        generation="team-generation",
        models=["openai-codex/model"],
        image="sha256:" + "a" * 64,
        image_override="",
        team_write=False,
        offline=True,
        verbose=False,
        agentws_host="127.0.0.1",
        intent="running",
        requested_port=0,
        team_roster="team",
        team_protocol=False,
        pi_default_provider="openai-codex",
        pi_default_model="model",
        project_name="demo",
        project_file=str(tmp_path / "project.cyclo"),
        project_description="Runtime test.",
        project_generation="project-generation",
        project_config=(
            "name demo\n"
            "description Runtime test.\n"
            "team /team ro\n"
            "mount source /workspace/source rw\n"
        ),
        project_mounts=[
            {"name": "source", "path": str(source), "mode": "rw"}
        ],
        runtime_version="0.2.0",
    )


def component_status(
    name: str,
    *,
    status: str = "running",
    health: str = "healthy",
    exit_code: int = 0,
    problem: str = "",
) -> DCompComponentStatus:
    return DCompComponentStatus(
        name=name,
        container_id="a" * 64,
        status=status,
        health=health,
        exit_code=exit_code,
        problem=problem,
        published_ports=(),
    )


def status(
    *components: DCompComponentStatus,
    operation: str = "",
    phase: str = "",
) -> DCompStatus:
    return DCompStatus(
        api_version=1,
        name="cyclo-test",
        desired=True,
        operational=not operation
        and all(
            item.status == "running" and item.health == "healthy"
            for item in components
        ),
        digest="a" * 64,
        operation=operation,
        phase=phase,
        networks=(),
        components=components,
    )


def test_empty_host_compiles_gateway_with_private_store_and_host_catalogue(
    tmp_path: Path,
) -> None:
    selected = runtime(tmp_path)

    system = selected.system(
        SimpleNamespace(gateway=image("a"), providers={}),
        (),
    )

    gateway = system.components[0]
    assert gateway.name == "gateway"
    assert gateway.outputs[0].service == PROVIDER_SERVICE
    assert gateway.volumes[0].name == "credentials"
    assert gateway.binds == ()
    assert gateway.ports[0].host_ip == "127.0.0.1"
    assert gateway.egress


def test_gateway_only_runtime_does_not_parse_malformed_host_configuration(
    tmp_path: Path,
) -> None:
    config = tmp_path / "host.conf"
    config.write_text("this is not a provider declaration\n", encoding="utf-8")
    observed = status(component_status("gateway"))
    dcomp = AppliedDComp(observed)
    selected = CycloRuntime(
        StateStore(tmp_path / "state"),
        config,
        dcomp=dcomp,  # type: ignore[arg-type]
        images=NoImages(),  # type: ignore[arg-type]
    )
    selected.build_gateway = (  # type: ignore[method-assign]
        lambda **_options: image("a")
    )

    assert selected.status() is observed
    assert selected.apply_gateway().status is observed
    assert dcomp.calls == ["status", "check", "up", "status"]

    with pytest.raises(CycloError, match="expected provider"):
        selected.host


def test_apply_delegates_pending_operation_recovery_to_dcomp_up(
    tmp_path: Path,
) -> None:
    observed = status(
        component_status("gateway", status="created", health="none"),
        operation="apply",
        phase="start",
    )
    dcomp = AppliedDComp(observed)
    selected = CycloRuntime(
        StateStore(tmp_path / "state"),
        tmp_path / "host.conf",
        dcomp=dcomp,  # type: ignore[arg-type]
        images=NoImages(),  # type: ignore[arg-type]
    )
    selected.build_gateway = (  # type: ignore[method-assign]
        lambda **_options: image("a")
    )

    assert selected.apply_gateway().status is observed
    assert dcomp.calls == ["check", "up", "status"]


def test_shutdown_delegates_realm_name_and_verifies_complete_absence(
    tmp_path: Path,
) -> None:
    absent = DCompStatus(
        api_version=1,
        name="cyclo-test",
        desired=False,
        operational=False,
        digest="",
        operation="",
        phase="",
        networks=(),
        components=(),
    )
    dcomp = AppliedDComp(absent)
    selected = CycloRuntime(
        StateStore(tmp_path / "state"),
        tmp_path / "missing-host.conf",
        dcomp=dcomp,  # type: ignore[arg-type]
        images=NoImages(),  # type: ignore[arg-type]
    )

    assert selected.shutdown() is absent
    assert dcomp.calls == [f"down:{selected.name}", "status"]


def test_shutdown_rejects_remaining_runtime_resources(tmp_path: Path) -> None:
    remaining = status(component_status("gateway"))
    dcomp = AppliedDComp(remaining)
    selected = CycloRuntime(
        StateStore(tmp_path / "state"),
        tmp_path / "host.conf",
        dcomp=dcomp,  # type: ignore[arg-type]
        images=NoImages(),  # type: ignore[arg-type]
    )

    with pytest.raises(CycloError, match="runtime resources remain"):
        selected.shutdown()


def test_provider_graph_and_openai_edge_compile_direct_interface_links(
    tmp_path: Path,
) -> None:
    for name in ("first", "second"):
        source = tmp_path / name
        source.mkdir()
        (source / "component.dcomp").write_text(
            "\n".join(
                (
                    f"docker {name}:dev",
                    f"input {PROVIDER_SERVICE} upstream",
                    f"output {PROVIDER_SERVICE} provider",
                    "",
                )
            ),
            encoding="utf-8",
        )
    selected = runtime(
        tmp_path,
        "\n".join(
            (
                f"provider first {tmp_path / 'first'} upstream=gateway.provider",
                f"provider second {tmp_path / 'second'} upstream=first.provider",
                "component openai bind=0.0.0.0 port=18080",
                "",
            )
        ),
    )

    system = selected.system(
        SimpleNamespace(
            gateway=image("a"),
            providers={"first": image("b"), "second": image("c")},
            components={"openai": image("d")},
        ),
        (),
    )

    assert [(link.consumer, link.provider) for link in system.links] == [
        ("first", "gateway"),
        ("second", "first"),
        ("openai", "second"),
    ]
    first = next(item for item in system.components if item.name == "first")
    second = next(item for item in system.components if item.name == "second")
    assert not first.egress
    assert first.ports == ()
    assert second.egress
    assert second.ports[0].container_port == 50051
    openai = next(item for item in system.components if item.name == "openai")
    assert openai.inputs[0].name == "provider"
    assert openai.outputs == ()
    assert openai.egress
    assert openai.ports[0].host_ip == "0.0.0.0"
    assert openai.ports[0].host_port == 18080
    assert openai.ports[0].container_port == 8080
    system.validate()


def test_build_host_includes_and_caches_declared_openai_image(
    tmp_path: Path,
) -> None:
    selected = runtime(tmp_path, "component openai\n")
    calls: list[str] = []
    selected._bind_docker = lambda: None  # type: ignore[method-assign]
    selected.prepare = lambda: None  # type: ignore[method-assign]
    selected.build_gateway = lambda **_options: image("a")  # type: ignore[method-assign]
    selected._host_component_image = (  # type: ignore[method-assign]
        lambda configured: calls.append(configured.name) or image("b")
    )

    first = selected.build_host()
    second = selected.build_host()
    rebuilt = selected.build_host(rebuild=True)

    assert set(first.components) == {"openai"}
    assert second.components["openai"] is first.components["openai"]
    assert rebuilt.components["openai"].id == image("b").id
    assert calls == ["openai", "openai"]


def test_bundled_pooler_compiles_as_an_intermediate_provider(
    tmp_path: Path,
) -> None:
    selected = runtime(
        tmp_path,
        "provider pool pooler upstream=gateway.provider "
        "-- account-a account-b\n",
    )

    system = selected.system(
        SimpleNamespace(
            gateway=image("a"),
            providers={"pool": image("b")},
            components={},
        ),
        (),
    )

    pool = next(item for item in system.components if item.name == "pool")
    assert pool.arguments == ("account-a", "account-b")
    assert pool.inputs[0].name == "upstream"
    assert pool.outputs[0].name == "provider"
    assert pool.egress
    assert [(link.consumer, link.provider) for link in system.links] == [
        ("pool", "gateway")
    ]
    system.validate()


def test_host_component_and_persisted_team_are_merged_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path, "component openai\n")
    selected.dcomp.executable = str(tmp_path / "dcomp")
    monkeypatch.setattr("cyclo.runtime.require_non_root_team_host", lambda: None)
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: f"unix://{tmp_path / 'docker.sock'}",
    )
    persisted = instance(tmp_path)

    system = selected.system(
        SimpleNamespace(
            gateway=image("a"),
            providers={},
            components={"openai": image("b")},
        ),
        (persisted,),
    )

    team = selected.component_for_instance(persisted.id)
    assert [item.name for item in selected.host.components] == ["openai"]
    assert selected.host.providers == ()
    assert {item.name for item in system.components} == {
        "gateway",
        "openai",
        team,
    }
    assert {(link.consumer, link.provider) for link in system.links} == {
        ("openai", "gateway"),
        (team, "gateway"),
    }
    system.validate()


def test_host_root_cannot_become_team_container_root(
    monkeypatch,
) -> None:
    monkeypatch.setattr("cyclo.team.image.os.getuid", lambda: 0)

    with pytest.raises(CycloError, match="host root"):
        require_non_root_team_host()


def test_build_team_rejects_host_root_before_binding_docker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected = runtime(tmp_path)
    monkeypatch.setattr("cyclo.team.image.os.getuid", lambda: 0)

    with pytest.raises(CycloError, match="host root"):
        selected.build_team(SimpleNamespace())


def test_pi_settings_are_an_immutable_template_outside_team_writable_state(
    tmp_path: Path,
) -> None:
    selected = runtime(tmp_path)
    selected_instance = instance(tmp_path)
    pi_root = selected.store.pi_root(selected_instance.id)
    pi_root.mkdir(parents=True)
    escape = tmp_path / "escape"
    escape.mkdir()
    (pi_root / "agent").symlink_to(escape, target_is_directory=True)

    files = materialize_instance(selected.store, selected_instance)
    component = make_team_component(selected.store, selected_instance)

    assert not files.pi_settings.is_relative_to(pi_root)
    assert stat.S_IMODE(files.pi_settings.stat().st_mode) == 0o444
    settings = json.loads(files.pi_settings.read_text(encoding="utf-8"))
    assert settings["defaultModel"] == "model"
    assert any(
        package.endswith("/pi-safe-compact")
        for package in settings["packages"]
    )
    assert not (escape / "settings.json").exists()
    settings_bind = next(
        bind
        for bind in component.binds
        if bind.target == PI_SETTINGS_TEMPLATE
    )
    assert settings_bind.source == files.pi_settings
    assert settings_bind.read_only


def test_target_readiness_ignores_unrelated_failed_component(
    tmp_path: Path,
) -> None:
    selected = runtime(tmp_path)
    selected_instance = SimpleNamespace(id="demo")
    selected.require_instances_ready(
        (selected_instance,),
        status(
            component_status("gateway"),
            component_status(selected.component_for_instance("demo")),
            component_status(
                "unrelated",
                status="exited",
                health="unhealthy",
                exit_code=1,
            ),
        ),
    )


def test_target_readiness_rejects_pending_operation(
    tmp_path: Path,
) -> None:
    selected = runtime(tmp_path)

    with pytest.raises(CycloError, match="still pending"):
        selected.require_instances_ready(
            (SimpleNamespace(id="demo"),),
            status(operation="up", phase="start"),
        )


def test_target_readiness_requires_provider_and_team(
    tmp_path: Path,
) -> None:
    selected = runtime(tmp_path)
    selected_instance = SimpleNamespace(id="demo")
    target = selected.component_for_instance("demo")

    with pytest.raises(CycloError, match="provider dependency"):
        selected.require_instances_ready(
            (selected_instance,),
            status(
                component_status(
                    "gateway",
                    status="exited",
                    health="unhealthy",
                    exit_code=1,
                    problem="startup failed",
                ),
                component_status(target),
            ),
        )

    with pytest.raises(CycloError, match="team 'demo'"):
        selected.require_instances_ready(
            (selected_instance,),
            status(component_status("gateway")),
        )


def test_persisted_instances_reject_cross_project_nested_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    selected.dcomp.executable = str(tmp_path / "dcomp")
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: f"unix://{tmp_path / 'docker.sock'}",
    )
    parent = tmp_path / "projects"
    child = parent / "child"
    child.mkdir(parents=True)
    first = instance(tmp_path, "first")
    first.project_file = str(tmp_path / "first.cyclo")
    first.project_generation = "first-generation"
    first.project_mounts[0]["path"] = str(parent)
    second = instance(tmp_path, "second")
    second.project_file = str(tmp_path / "second.cyclo")
    second.project_generation = "second-generation"
    second.project_mounts[0]["path"] = str(child)

    with pytest.raises(CycloError, match="must not contain one another"):
        selected.validate_instances((first, second))


def test_persisted_instances_allow_exact_project_root_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = runtime(tmp_path)
    selected.dcomp.executable = str(tmp_path / "dcomp")
    monkeypatch.setattr(
        "cyclo.state.local_docker_endpoint",
        lambda: f"unix://{tmp_path / 'docker.sock'}",
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    first = instance(tmp_path, "first")
    first.project_file = str(tmp_path / "first.cyclo")
    first.project_generation = "first-generation"
    first.project_mounts[0]["path"] = str(shared)
    second = instance(tmp_path, "second")
    second.project_file = str(tmp_path / "second.cyclo")
    second.project_generation = "second-generation"
    second.project_mounts[0]["path"] = str(shared)

    selected.validate_instances((first, second))
