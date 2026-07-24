from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cyclo.providers as providers_module
from cyclo.component import (
    COMPONENT_INTERFACE,
    COMPONENT_SOCKET,
    PROVIDER_INTERFACE,
    Component,
    ComponentStatus,
    Declaration,
)
from cyclo.component_runtime import (
    LABEL_COMPONENT_CLASS,
    LABEL_OWNED,
)
from cyclo.errors import CycloError
from cyclo.installation import (
    LABEL_INSTANCE,
    LABEL_SYSTEM,
    provider_name,
)
from cyclo.providers import (
    ProviderConnection,
    ProviderSystem,
    load_provider_configuration,
)


IMAGE_ID = f"sha256:{'a' * 64}"
CONTAINER_ID = "b" * 64


def _model(identifier: str) -> dict[str, object]:
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


def _write_component(
    root: Path,
    directory_name: str,
    *,
    component_name: str = "passthrough",
    requirements: tuple[tuple[str, str], ...] = (
        ("upstream", PROVIDER_INTERFACE),
    ),
) -> Path:
    source = root / directory_name
    source.mkdir(parents=True)
    (source / "Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    declaration = [
        f"component {component_name}",
        f"provide {COMPONENT_INTERFACE}",
        f"provide {PROVIDER_INTERFACE}",
        *(
            f"require {name} {service}"
            for name, service in requirements
        ),
        "",
    ]
    (source / "component.conf").write_text(
        "\n".join(declaration),
        encoding="utf-8",
    )
    return source


def _status(
    name: str,
    *,
    present: bool = True,
    running: bool = True,
    current: bool = True,
    ready: bool = True,
    error: str = "",
) -> ComponentStatus:
    return ComponentStatus(
        name,
        "gateway" if name == "gateway" else "passthrough",
        IMAGE_ID,
        CONTAINER_ID if present else None,
        running,
        "running" if running else ("stopped" if present else "absent"),
        "healthy" if running else "missing",
        current,
        "ready" if ready else "not-ready",
        error,
    )


class FakeGateway:
    def __init__(self, root: Path, controller) -> None:
        self.controller = controller
        self.socket_dir = root / "gateway"
        self.socket_path = self.socket_dir / COMPONENT_SOCKET
        self.component = Component(
            "gateway",
            Declaration(
                "gateway",
                (COMPONENT_INTERFACE, PROVIDER_INTERFACE),
                (),
            ),
            root,
            root,
            "gateway:latest",
            "gateway",
            "0123456789ab",
            (),
            (),
            "bridge",
            self.socket_path,
            component_class="gateway",
            preserve_volumes=True,
        )

    def status(self, *, error: str = "") -> ComponentStatus:
        return _status("gateway", error=error)

    def start(self) -> ComponentStatus:
        return self.status()

    def build(self) -> str:
        return IMAGE_ID

    def stop(self) -> bool:
        return True

    def restart(self) -> ComponentStatus:
        return self.status()

    def logs(self, _lines: int = 80) -> str:
        return ""


def test_host_configuration_is_ordered_and_gateway_is_only_root(
    tmp_path: Path,
) -> None:
    first = _write_component(tmp_path, "first")
    second = _write_component(tmp_path, "second")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first context=.. upstream=gateway -- mode=plain\n"
        "provider second ./second upstream=first\n",
        encoding="utf-8",
    )

    parsed = load_provider_configuration(config)

    assert [provider.name for provider in parsed.providers] == [
        "first",
        "second",
    ]
    assert parsed.providers[0].source == first
    assert parsed.providers[0].build_context == tmp_path
    assert parsed.providers[0].arguments == ("mode=plain",)
    assert parsed.providers[1].source == second
    assert parsed.providers[1].bindings == (("upstream", "first"),)

    config.write_text(
        "provider first ./first upstream=later\n",
        encoding="utf-8",
    )
    with pytest.raises(CycloError, match="unknown or later provider"):
        load_provider_configuration(config)

    root = _write_component(tmp_path, "root", requirements=())
    assert root.is_dir()
    config.write_text("provider root ./root\n", encoding="utf-8")
    with pytest.raises(CycloError, match="must require an upstream"):
        load_provider_configuration(config)


def test_host_configuration_rejects_symlinked_component_files(
    tmp_path: Path,
) -> None:
    source = _write_component(tmp_path, "source")
    declaration = source / "component.conf"
    real = source / "real.conf"
    declaration.rename(real)
    declaration.symlink_to(real.name)
    config = tmp_path / "host.conf"
    config.write_text(
        "provider pass ./source upstream=gateway\n",
        encoding="utf-8",
    )

    with pytest.raises(CycloError, match="not a regular file"):
        load_provider_configuration(config)


def test_provider_components_receive_only_named_socket_capabilities(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "second")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider second ./second upstream=first\n",
        encoding="utf-8",
    )

    class Controller:
        pass

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    first, second = providers.provider_components
    assert first.network == second.network == "none"
    assert [mount.destination for mount in first.mounts] == [
        "/run/cyclo",
        "/run/cyclo/requirements/upstream",
    ]
    assert first.mounts[1].source == str(gateway.socket_dir)
    assert first.mounts[1].read_only
    assert second.mounts[1].source == str(
        providers.socket_dir("first")
    )
    assert all("/var/lib/cyclo-gateway" not in mount.destination for mount in first.mounts)


def test_failed_provider_keeps_gateway_selected_and_exposes_error(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "broken")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider broken ./broken upstream=gateway\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(
                component.name,
                present=False,
                running=False,
                ready=False,
                error=error,
            )

        def start(self, _component: Component) -> ComponentStatus:
            raise CycloError("deliberate provider failure")

        def call(self, arguments, **_options):
            assert arguments[:2] == ["container", "ls"]
            return subprocess.CompletedProcess(arguments, 0, "", "")

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    connection = providers.start()

    assert connection.socket_path == gateway.socket_path
    broken = connection.components[1]
    assert broken.name == "broken"
    assert not broken.works
    assert broken.error == "deliberate provider failure"


def test_unavailable_dependency_does_not_inspect_or_abort_its_dependant(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "broken")
    _write_component(tmp_path, "dependent")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider broken ./broken upstream=gateway\n"
        "provider dependent ./dependent upstream=broken\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            if component.name == "dependent":
                raise CycloError("dependent container has invalid ownership")
            return _status(
                component.name,
                present=False,
                running=False,
                ready=False,
                error=error,
            )

        def start(self, component: Component) -> ComponentStatus:
            if component.name == "broken":
                raise CycloError("deliberate provider failure")
            raise AssertionError("dependent provider must not be started")

        def stop(self, *_args) -> bool:
            raise AssertionError("unverified dependent must not be removed")

        def call(self, arguments, **_options):
            assert arguments[:2] == ["container", "ls"]
            return subprocess.CompletedProcess(arguments, 0, "", "")

    controller = Controller()
    root = tmp_path / "components"
    providers = ProviderSystem(
        root,
        config,
        gateway=FakeGateway(root, controller),  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    connection = providers.start()

    assert connection.socket_path == providers.gateway.socket_path
    assert [status.name for status in connection.components] == [
        "gateway",
        "broken",
        "dependent",
    ]
    assert connection.components[1].error == "deliberate provider failure"
    assert "required component unavailable: broken" in connection.components[2].error
    assert "dependent container has invalid ownership" in connection.components[2].error


def test_unavailable_dependency_cleanup_failure_does_not_abort_fallback(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "broken")
    _write_component(tmp_path, "dependent")
    _write_component(tmp_path, "healthy")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider broken ./broken upstream=gateway\n"
        "provider dependent ./dependent upstream=broken\n"
        "provider healthy ./healthy upstream=gateway\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            if component.name == "dependent":
                return _status(component.name, error=error)
            if component.name == "healthy":
                return _status(component.name, error=error)
            return _status(
                component.name,
                present=False,
                running=False,
                ready=False,
                error=error,
            )

        def start(self, component: Component) -> ComponentStatus:
            if component.name == "broken":
                raise CycloError("deliberate provider failure")
            if component.name == "healthy":
                return _status(component.name)
            raise AssertionError("dependent provider must not be started")

        def stop(self, component: Component, expected_id: str) -> bool:
            assert component.name == "dependent"
            assert expected_id == CONTAINER_ID
            raise CycloError("Docker refused dependent cleanup")

        def call(self, arguments, **_options):
            assert arguments[:2] == ["container", "ls"]
            return subprocess.CompletedProcess(arguments, 0, "", "")

    controller = Controller()
    root = tmp_path / "components"
    providers = ProviderSystem(
        root,
        config,
        gateway=FakeGateway(root, controller),  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    connection = providers.start()

    assert connection.socket_path == providers.socket_path("healthy")
    by_name = {status.name: status for status in connection.components}
    assert by_name["broken"].error == "deliberate provider failure"
    assert "required component unavailable: broken" in by_name["dependent"].error
    assert "cleanup failed: Docker refused dependent cleanup" in by_name["dependent"].error
    assert by_name["healthy"].works


def test_last_working_provider_is_selected_when_a_later_one_fails(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "broken")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider broken ./broken upstream=first\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(
                component.name,
                ready=component.name == "first",
                error=error,
            )

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    connection = providers.connection()

    assert connection.socket_path == providers.socket_path("first")


def test_stop_runs_in_reverse_order_then_removes_owned_stray(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "second")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider second ./second upstream=first\n",
        encoding="utf-8",
    )

    class Controller:
        def __init__(self) -> None:
            self.events: list[tuple[object, ...]] = []
            self.system = ""

        def stop(self, component: Component, *_args) -> bool:
            self.events.append(("stop", component.name))
            return True

        def call(self, arguments, **_options):
            command = list(arguments)
            if command[:2] == ["container", "ls"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    f"{CONTAINER_ID}\n",
                    "",
                )
            self.events.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        def inspect(
            self,
            kind: str,
            reference: str,
            *,
            missing: bool = True,
        ):
            assert kind == "container"
            assert reference == CONTAINER_ID
            return {
                "Id": CONTAINER_ID,
                "Name": f"/{provider_name(self.system, 'orphan')}",
                "Config": {
                    "Labels": {
                        LABEL_OWNED: "1",
                        LABEL_SYSTEM: self.system,
                        LABEL_INSTANCE: "orphan",
                        LABEL_COMPONENT_CLASS: "provider",
                    }
                },
                "State": {"Running": True, "Status": "running"},
            }

        @staticmethod
        def container_id(info) -> str:
            return str(info["Id"])

        @staticmethod
        def labels(info) -> dict[str, str]:
            return dict(info["Config"]["Labels"])

        @staticmethod
        def container_state(_info) -> str:
            return "running"

        def remove_verified_container(
            self,
            identifier: str,
            state: str,
            *,
            preserve_volumes: bool,
        ) -> None:
            assert state == "running"
            assert not preserve_volumes
            self.call(["stop", "--timeout", "10", identifier])
            self.call(["rm", "--volumes", identifier])

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )
    controller.system = providers.system

    assert providers.stop() == ("second", "first", "orphan")
    assert controller.events[:2] == [
        ("stop", "second"),
        ("stop", "first"),
    ]
    assert ("stop", "--timeout", "10", CONTAINER_ID) in controller.events
    assert ("rm", "--volumes", CONTAINER_ID) in controller.events


def test_empty_model_catalogue_is_an_empty_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Controller:
        pass

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        tmp_path / "host.conf",
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        providers,
        "statuses",
        lambda: (_status("gateway"),),
    )
    monkeypatch.setattr(
        providers_module,
        "connect_unary",
        lambda *_args, **_kwargs: {},
    )

    connection, catalogue = providers.catalogue(providers.connection())
    assert connection.socket_path == gateway.socket_path
    assert catalogue == {"models": []}


def test_catalogue_falls_back_when_a_ready_outer_provider_cannot_list_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "outer")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider outer ./outer upstream=first\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(component.name, error=error)

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )
    calls: list[Path] = []

    def list_models(socket_path, *_args, **_options):
        calls.append(socket_path)
        if socket_path == providers.socket_path("outer"):
            raise CycloError("outer catalogue crashed")
        return {"models": [_model("first/model")]}

    monkeypatch.setattr(providers_module, "connect_unary", list_models)

    connection, document = providers.catalogue(providers.connection())

    assert calls == [
        providers.socket_path("outer"),
        providers.socket_path("first"),
    ]
    assert connection.socket_path == providers.socket_path("first")
    assert document == {"models": [_model("first/model")]}
    outer = next(
        status
        for status in connection.components
        if status.name == "outer"
    )
    assert not outer.works
    assert "outer catalogue crashed" in outer.error


@pytest.mark.parametrize(
    "invalid_models",
    [
        [{}],
        [{"id": ""}],
        [_model("outer/model"), _model("outer/model")],
        [{"id": "outer/model"}],
        [_model("_legacy/model")],
        [_model(f"{'a' * 65}/model")],
        [_model("outer/bad\u0085model")],
        [_model(f"outer/{'😀' * 513}")],
        [_model("outer/\ud800")],
    ],
)
def test_catalogue_falls_back_from_invalid_outer_model_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_models: list[dict[str, object]],
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "outer")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider outer ./outer upstream=first\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(component.name, error=error)

    controller = Controller()
    root = tmp_path / "components"
    providers = ProviderSystem(
        root,
        config,
        gateway=FakeGateway(root, controller),  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    def list_models(socket_path, *_args, **_options):
        if socket_path == providers.socket_path("outer"):
            return {"models": invalid_models}
        return {"models": [_model("first/model")]}

    monkeypatch.setattr(providers_module, "connect_unary", list_models)

    connection, document = providers.catalogue(providers.connection())

    assert connection.socket_path == providers.socket_path("first")
    assert document == {"models": [_model("first/model")]}
    assert not connection.components[-1].works
    assert "invalid" in connection.components[-1].error


def test_catalogue_keeps_a_structurally_valid_non_pi_outer_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_component(tmp_path, "outer")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider outer ./outer upstream=gateway\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(component.name, error=error)

    controller = Controller()
    root = tmp_path / "components"
    providers = ProviderSystem(
        root,
        config,
        gateway=FakeGateway(root, controller),  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )
    document = {
        "models": [
            {
                "id": "future/model",
                "inferenceFormat": "future-runtime@1",
            }
        ]
    }
    monkeypatch.setattr(
        providers_module,
        "connect_unary",
        lambda *_args, **_options: document,
    )

    connection, selected = providers.catalogue(providers.connection())

    assert connection.socket_path == providers.socket_path("outer")
    assert selected == document


def test_component_start_rejects_an_unavailable_requirement(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "pass")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider pass ./pass upstream=gateway\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(component.name, error=error)

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    gateway.status = lambda **_options: _status(
        "gateway",
        present=False,
        running=False,
        ready=False,
    )
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    with pytest.raises(CycloError, match="required component unavailable"):
        providers.start_component("pass")


def test_component_status_and_start_do_not_inspect_unrelated_components(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "unrelated")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider unrelated ./unrelated upstream=gateway\n",
        encoding="utf-8",
    )

    class Controller:
        def __init__(self) -> None:
            self.inspected: list[str] = []

        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            self.inspected.append(component.name)
            if component.name == "unrelated":
                raise CycloError("unrelated component is malformed")
            return _status(component.name, error=error)

        def start(self, component: Component) -> ComponentStatus:
            return _status(component.name)

    controller = Controller()
    root = tmp_path / "components"
    providers = ProviderSystem(
        root,
        config,
        gateway=FakeGateway(root, controller),  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    assert providers.status_component("first").works
    assert controller.inspected == ["first"]
    assert providers.start_component("first").works
    assert controller.inspected == ["first"]


def test_component_start_does_not_prepare_an_unrelated_socket_tree(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "unrelated")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider unrelated ./unrelated upstream=gateway\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(component.name, error=error)

        def start(self, component: Component) -> ComponentStatus:
            return _status(component.name)

    controller = Controller()
    root = tmp_path / "components"
    providers = ProviderSystem(
        root,
        config,
        gateway=FakeGateway(root, controller),  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )
    providers.sockets_root.mkdir(parents=True)
    unrelated_target = tmp_path / "outside"
    unrelated_target.mkdir()
    providers.socket_dir("unrelated").symlink_to(
        unrelated_target,
        target_is_directory=True,
    )

    assert providers.start_component("first").works
    assert providers.socket_dir("unrelated").is_symlink()


def test_component_start_checks_transitive_dependencies(
    tmp_path: Path,
) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "second")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider second ./second upstream=first\n",
        encoding="utf-8",
    )

    class Controller:
        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return _status(component.name, error=error)

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    gateway.status = lambda **_options: _status(
        "gateway",
        present=False,
        running=False,
        ready=False,
    )
    providers = ProviderSystem(
        root,
        config,
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    with pytest.raises(
        CycloError,
        match=r"required component unavailable: gateway, first",
    ):
        providers.start_component("second")


def test_component_start_rejects_an_unknown_name_cleanly(
    tmp_path: Path,
) -> None:
    class Controller:
        pass

    controller = Controller()
    root = tmp_path / "components"
    gateway = FakeGateway(root, controller)
    providers = ProviderSystem(
        root,
        tmp_path / "host.conf",
        gateway=gateway,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
    )

    with pytest.raises(CycloError, match="unknown configured component"):
        providers.start_component("missing")
