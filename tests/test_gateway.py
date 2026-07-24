from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from cyclo.component import Component, ComponentStatus
from cyclo.errors import CycloError
from cyclo.gateway import Gateway


IMAGE_ID = f"sha256:{'a' * 64}"
CONTAINER_ID = "b" * 64


def _status(
    gateway: Gateway,
    *,
    current: bool = True,
) -> ComponentStatus:
    return ComponentStatus(
        "gateway",
        "gateway",
        IMAGE_ID,
        CONTAINER_ID,
        True,
        "running",
        "healthy",
        current,
        "ready",
    )


def test_gateway_usage_mounts_credential_store_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Controller:
        def __init__(self) -> None:
            self.gateway: Gateway | None = None
            self.commands: list[list[str]] = []

        def require_image(self, _component: Component) -> str:
            return IMAGE_ID

        def inspect(
            self,
            kind: str,
            _reference: str,
            **_options: object,
        ):
            assert kind == "volume"
            assert self.gateway is not None
            return {
                "Name": self.gateway.store_volume,
                "Driver": "local",
                "Scope": "local",
                "Labels": self.gateway._volume_labels(),
                "Options": {},
            }

        def call(self, arguments, **_options):
            self.commands.append(list(arguments))
            return subprocess.CompletedProcess(
                arguments,
                0,
                '{"totals":{"requests":0}}\n',
                "",
            )

    controller = Controller()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,  # type: ignore[arg-type]
    )
    controller.gateway = gateway

    assert gateway.usage() == {"totals": {"requests": 0}}
    command = controller.commands[-1]
    mount = command[command.index("--mount") + 1]
    assert mount.endswith("/var/lib/cyclo-gateway,readonly")

    gateway._tool(["login", "openai"], volume=True, config=True)
    login_command = controller.commands[-1]
    mounts = [
        login_command[index + 1]
        for index, item in enumerate(login_command)
        if item == "--mount"
    ]
    assert (
        f"type=bind,src={gateway.config_dir},"
        "dst=/etc/cyclo-gateway,readonly"
    ) in mounts


def test_gateway_login_ensures_then_restarts_after_credential_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Controller:
        def ensure_image(self, _component: Component) -> str:
            events.append("ensure")
            return IMAGE_ID

        def status(
            self,
            _component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            events.append("status")
            return ComponentStatus(
                "gateway",
                "gateway",
                IMAGE_ID,
                CONTAINER_ID,
                True,
                "running",
                "healthy",
                False,
                "ready",
            )

        def restart(
            self,
            _component: Component,
        ) -> ComponentStatus:
            events.append("restart")
            return _status(gateway)

    controller = Controller()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,  # type: ignore[arg-type]
    )
    published = _status(gateway)
    monkeypatch.setattr(gateway, "store_ready", lambda **_options: True)
    monkeypatch.setattr(
        gateway,
        "_require_exclusive_store",
        lambda allowed=None: events.append(("exclusive", allowed)),
    )
    monkeypatch.setattr(
        gateway,
        "_tool",
        lambda command, **_options: events.append(
            ("tool", tuple(command), _options.get("config"))
        ),
    )
    monkeypatch.setattr(
        gateway,
        "status",
        lambda **_options: published,
    )

    assert gateway.login(["anthropic"]) is published
    assert events == [
        "ensure",
        "status",
        ("exclusive", CONTAINER_ID),
        ("tool", ("login", "anthropic"), True),
        "restart",
    ]


def test_gateway_failed_login_does_not_stop_the_running_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )
    controller.status.return_value = _status(gateway)
    controller.ensure_image.return_value = IMAGE_ID
    monkeypatch.setattr(gateway, "_prepare", Mock())
    monkeypatch.setattr(gateway, "store_ready", Mock(return_value=True))
    monkeypatch.setattr(gateway, "_require_exclusive_store", Mock())
    monkeypatch.setattr(
        gateway,
        "_tool",
        Mock(side_effect=CycloError("candidate catalogue is invalid")),
    )
    with pytest.raises(CycloError, match="candidate catalogue"):
        gateway.login(["unknown", "--api-key-stdin"])

    controller.restart.assert_not_called()


def test_gateway_readiness_failure_reports_cleanup_failure(
    tmp_path: Path,
) -> None:
    controller = Mock()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )
    status = ComponentStatus(
        "gateway",
        "gateway",
        IMAGE_ID,
        CONTAINER_ID,
        True,
        "running",
        "healthy",
        True,
        "not-ready",
        "invalid gateway socket",
    )
    controller.stop.side_effect = CycloError("Docker removal failed")

    with pytest.raises(
        CycloError,
        match="invalid gateway socket; cleanup failed: Docker removal failed",
    ):
        gateway._require_working(status)

    controller.stop.assert_called_once_with(gateway.component, CONTAINER_ID)


def test_destroy_store_rejects_a_foreign_volume_user_before_stopping(
    tmp_path: Path,
) -> None:
    foreign_id = "c" * 64

    class Controller:
        def __init__(self) -> None:
            self.gateway: Gateway | None = None
            self.stopped = False

        def inspect(
            self,
            kind: str,
            reference: str,
            **_options: object,
        ):
            assert self.gateway is not None
            if kind == "volume":
                return {
                    "Name": self.gateway.store_volume,
                    "Driver": "local",
                    "Scope": "local",
                    "Labels": self.gateway._volume_labels(),
                    "Options": {},
                }
            assert kind == "container"
            return {"Id": reference}

        @staticmethod
        def container_id(info) -> str:
            return str(info["Id"])

        def status(
            self,
            _component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            assert self.gateway is not None
            return _status(self.gateway)

        def call(self, arguments, **_options):
            assert arguments[:2] == ["container", "ls"]
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"{CONTAINER_ID}\n{foreign_id}\n",
                "",
            )

        def stop(self, _component: Component) -> bool:
            self.stopped = True
            return True

    controller = Controller()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,  # type: ignore[arg-type]
    )
    controller.gateway = gateway

    with pytest.raises(CycloError, match="mounted by another container"):
        gateway.destroy_store()
    assert not controller.stopped


def test_gateway_status_rejects_foreign_store_metadata(
    tmp_path: Path,
) -> None:
    class Controller:
        def status(
            self,
            _component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            return ComponentStatus(
                "gateway",
                "gateway",
                None,
                None,
                False,
                "absent",
                "missing",
                False,
                "unreachable",
            )

        @staticmethod
        def inspect(
            kind: str,
            _reference: str,
            **_options: object,
        ):
            assert kind == "volume"
            return {
                "Name": "foreign",
                "Driver": "local",
                "Scope": "local",
                "Labels": {},
                "Options": {},
            }

    gateway = Gateway(
        tmp_path / "components",
        controller=Controller(),  # type: ignore[arg-type]
    )

    with pytest.raises(CycloError, match="refusing foreign"):
        gateway.status()
