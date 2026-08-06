from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclo.dcomp import DCompComponentStatus, DCompStatus
from cyclo.errors import CycloError
from cyclo.gateway_admin import GatewayAdmin
from cyclo.runtime import CycloRuntime
from cyclo.state import StateStore


VOLUME_NAME = "opaque-volume-from-dcomp"


def component(
    name: str = "gateway",
    *,
    container_id: str = "a" * 64,
    status: str = "running",
    health: str = "healthy",
    problem: str = "",
) -> DCompComponentStatus:
    return DCompComponentStatus(
        name=name,
        container_id=container_id,
        status=status,
        health=health,
        exit_code=0,
        problem=problem,
        published_ports=(),
    )


def status(
    *components: DCompComponentStatus,
    desired: bool = True,
    operation: str = "",
) -> DCompStatus:
    return DCompStatus(
        api_version=1,
        name="cyclo-test",
        desired=desired,
        operational=desired
        and not operation
        and all(
            item.status == "running" and item.health == "healthy"
            for item in components
        ),
        digest="a" * 64 if desired else "",
        operation=operation,
        phase="applying" if operation else "",
        networks=(),
        components=components,
    )


class FakeImages:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def command(self, arguments, **options):
        selected = list(arguments)
        self.calls.append((selected, dict(options)))
        if selected[:3] == ["container", "ls", "--all"]:
            return subprocess.CompletedProcess(selected, 0, "", "")
        return subprocess.CompletedProcess(selected, 0, "", "")


class FakeDComp:
    def __init__(self, *, volume_available: bool = True) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.volume_available = volume_available

    def volume(self, system: str, component: str, logical_name: str) -> str:
        self.calls.append(("volume", system, component, logical_name))
        if not self.volume_available:
            raise CycloError("dcomp volume failed with status 1: volume is absent")
        return VOLUME_NAME

    def restart(self, name: str, *components: str) -> None:
        self.calls.append(("restart", name, *components))

    def resume(self, name: str) -> None:
        self.calls.append(("resume", name))

    def down(self, name: str) -> None:
        self.calls.append(("down", name))


class RuntimeDComp(FakeDComp):
    executable = "/usr/bin/dcomp"

    def __init__(self, observed: DCompStatus) -> None:
        super().__init__()
        self.observed = observed

    def status(self, _name: str) -> DCompStatus:
        return self.observed


class FakeRuntime:
    def __init__(
        self,
        *statuses: DCompStatus,
        volume_available: bool = True,
        waited: DCompStatus | None = None,
    ) -> None:
        self._statuses = list(statuses) or [status(component())]
        self._waited = waited or status(component())
        self.status_calls = 0
        self.apply_gateway_calls = 0
        self.images = FakeImages()
        self.dcomp = FakeDComp(volume_available=volume_available)
        self.store = SimpleNamespace(list=lambda: [], system="123456789abc")
        self.name = "cyclo-test"

    def status(self) -> DCompStatus:
        index = min(self.status_calls, len(self._statuses) - 1)
        self.status_calls += 1
        return self._statuses[index]

    def apply_gateway(self):
        self.apply_gateway_calls += 1
        return SimpleNamespace(status=self._waited)

    def wait_status(self) -> DCompStatus:
        return self._waited

    def build_gateway(self):
        return SimpleNamespace(id="sha256:" + "a" * 64)


def test_api_key_environment_is_passed_only_on_stdin() -> None:
    calls: list[tuple[tuple[str, ...], str | None, bool]] = []
    runtime = FakeRuntime(status(component()))
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    def tool(_image, command, **options):
        calls.append(
            (
                tuple(command),
                options["input_data"],
                options["interactive"],
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    admin._tool = tool  # type: ignore[method-assign]
    admin.login(
        ("openai", "--api-key-env", "SECRET"),
        environment={"SECRET": "private"},
    )

    assert calls == [
        (("login", "openai", "--api-key-stdin"), "private\n", True),
    ]
    assert runtime.dcomp.calls == [
        ("volume", "cyclo-test", "gateway", "credentials"),
        ("restart", "cyclo-test", "gateway"),
    ]
    assert runtime.apply_gateway_calls == 0
    assert "private" not in repr(calls[0][0])


def test_prepare_store_applies_only_gateway_for_fresh_installation() -> None:
    runtime = FakeRuntime(status(desired=False))
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    assert admin._prepare_store() == VOLUME_NAME

    assert runtime.apply_gateway_calls == 1
    assert runtime.dcomp.calls == [
        ("volume", "cyclo-test", "gateway", "credentials")
    ]
    assert runtime.images.calls == []


def test_prepare_store_resumes_pending_operation_before_volume_resolution() -> None:
    runtime = FakeRuntime(
        status(component(), operation="up"),
        status(component()),
    )
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    assert admin._prepare_store() == VOLUME_NAME

    assert runtime.status_calls == 2
    assert runtime.dcomp.calls == [
        ("resume", "cyclo-test"),
        ("volume", "cyclo-test", "gateway", "credentials"),
    ]
    assert runtime.apply_gateway_calls == 0
    assert runtime.images.calls == []


@pytest.mark.parametrize(
    "gateway",
    (
        None,
        component(container_id=""),
        component(status="missing"),
    ),
)
def test_prepare_store_rejects_desired_system_without_gateway_container(
    gateway: DCompComponentStatus | None,
) -> None:
    components = () if gateway is None else (gateway,)
    runtime = FakeRuntime(status(*components))
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    with pytest.raises(CycloError, match="gateway component is absent"):
        admin._prepare_store()

    assert runtime.apply_gateway_calls == 0
    assert runtime.images.calls == []


def test_prepare_store_rejects_missing_gateway_volume() -> None:
    runtime = FakeRuntime(status(component()), volume_available=False)
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    with pytest.raises(CycloError, match="dcomp volume failed"):
        admin._prepare_store()

    assert runtime.apply_gateway_calls == 0


def test_login_reports_gateway_that_does_not_recover_after_restart() -> None:
    runtime = FakeRuntime(
        status(component()),
        waited=status(
            component(
                status="exited",
                health="unhealthy",
                problem="credential catalogue failed",
            )
        ),
    )
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]
    admin._tool = lambda *_args, **_kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
        (), 0, "", ""
    )

    with pytest.raises(
        CycloError,
        match="gateway did not become ready.*credential catalogue failed",
    ):
        admin.login(("openai", "--api-key-stdin"))

    assert runtime.dcomp.calls == [
        ("volume", "cyclo-test", "gateway", "credentials"),
        ("restart", "cyclo-test", "gateway"),
    ]


def test_restart_prepares_and_restarts_only_gateway() -> None:
    runtime = FakeRuntime(status(component()))
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    admin.restart()

    assert runtime.apply_gateway_calls == 0
    assert runtime.dcomp.calls == [
        ("volume", "cyclo-test", "gateway", "credentials"),
        ("restart", "cyclo-test", "gateway"),
    ]
    assert runtime.status_calls == 1


def test_restart_reports_gateway_that_does_not_recover() -> None:
    runtime = FakeRuntime(
        status(component()),
        waited=status(
            component(
                status="exited",
                health="unhealthy",
                problem="catalogue failed",
            )
        ),
    )
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    with pytest.raises(
        CycloError,
        match="gateway did not become ready.*catalogue failed",
    ):
        admin.restart()


def test_destroy_store_resolves_name_without_reapplying_gateway() -> None:
    runtime = FakeRuntime(status(desired=False))
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    assert admin.destroy_store(VOLUME_NAME) == VOLUME_NAME

    assert runtime.status_calls == 0
    assert runtime.apply_gateway_calls == 0
    assert runtime.dcomp.calls == [
        ("volume", "cyclo-test", "gateway", "credentials"),
        ("down", "cyclo-test"),
    ]
    assert runtime.images.calls == [
        (["volume", "rm", "--", VOLUME_NAME], {"check": False})
    ]


def test_destroy_store_rejects_wrong_resolved_name_before_mutation() -> None:
    runtime = FakeRuntime(status(desired=False))
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    with pytest.raises(CycloError, match=VOLUME_NAME):
        admin.destroy_store("guessed-volume-name")

    assert runtime.dcomp.calls == [
        ("volume", "cyclo-test", "gateway", "credentials")
    ]
    assert runtime.images.calls == []


def test_interactive_gateway_tool_attaches_container_stdin() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Images:
        def command(self, arguments, **options):
            calls.append((list(arguments), options))
            return subprocess.CompletedProcess(arguments, 0, "", "")

    runtime = SimpleNamespace(
        images=Images(),
        store=SimpleNamespace(system="123456789abc"),
        name="cyclo-test",
    )
    admin = GatewayAdmin(runtime)  # type: ignore[arg-type]

    admin._tool(
        SimpleNamespace(id="sha256:" + "a" * 64),
        ("login", "openai", "--api-key-stdin"),
        volume=VOLUME_NAME,
        input_data="private\n",
        capture=False,
        interactive=True,
    )

    arguments, options = calls[-1]
    assert arguments[:2] == ["run", "--interactive"]
    assert arguments[arguments.index("--mount") + 1] == (
        f"type=volume,src={VOLUME_NAME},dst=/var/lib/cyclo-gateway"
    )
    assert options["input_data"] == "private\n"


def test_gateway_administration_ignores_malformed_provider_configuration(
    tmp_path: Path,
) -> None:
    config = tmp_path / "host.conf"
    config.write_text("invalid host configuration\n", encoding="utf-8")
    observed = status(component())
    dcomp = RuntimeDComp(observed)

    class Images(FakeImages):
        endpoint = None

        def command(self, arguments, **options):
            selected = list(arguments)
            result = super().command(selected, **options)
            if "providers" in selected:
                return subprocess.CompletedProcess(
                    selected, 0, "openai  OpenAI API\n", ""
                )
            if "usage" in selected:
                return subprocess.CompletedProcess(selected, 0, "{}\n", "")
            return result

    images = Images()
    runtime = CycloRuntime(
        StateStore(tmp_path / "state"),
        config,
        dcomp=dcomp,  # type: ignore[arg-type]
        images=images,  # type: ignore[arg-type]
    )
    runtime.build_gateway = lambda **_options: SimpleNamespace(  # type: ignore[method-assign]
        id="sha256:" + "a" * 64
    )
    admin = GatewayAdmin(runtime)

    assert runtime.status() is observed
    assert admin.providers() == "openai  OpenAI API"
    assert admin.usage() == {}
    admin.restart()
    admin.login(("openai", "--api-key-stdin"))
    assert dcomp.calls == [
        ("volume", runtime.name, "gateway", "credentials"),
        ("volume", runtime.name, "gateway", "credentials"),
        ("restart", runtime.name, "gateway"),
        ("volume", runtime.name, "gateway", "credentials"),
        ("restart", runtime.name, "gateway"),
    ]

    with pytest.raises(CycloError, match="expected provider"):
        runtime.host
