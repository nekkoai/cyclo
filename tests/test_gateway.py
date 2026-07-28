from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from cyclo.component import Component, ComponentStatus
from cyclo.component_runtime import ComponentController
from cyclo.errors import CycloError
from cyclo.gateway import LABEL_TOOL, Gateway


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

        def status(
            self,
            _component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            assert self.gateway is not None
            return _status(self.gateway)

        def call(self, arguments, **_options):
            self.commands.append(list(arguments))
            if arguments[0] == "create":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    f"{CONTAINER_ID}\n",
                    "",
                )
            if arguments[0] == "start":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    '{"totals":{"requests":0}}\n',
                    "",
                )
            return subprocess.CompletedProcess(
                arguments,
                0,
                "",
                "",
            )

        def stop(
            self,
            _component: Component,
            _identifier: str | None = None,
        ) -> bool:
            return True

    controller = Controller()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,  # type: ignore[arg-type]
    )
    controller.gateway = gateway

    assert gateway.usage() == {"totals": {"requests": 0}}
    command = next(
        command
        for command in controller.commands
        if command[0] == "create" and command[-1] == "usage"
    )
    mount = command[command.index("--mount") + 1]
    assert mount.endswith("/var/lib/cyclo-gateway,readonly")

    gateway._tool(["login", "openai"], volume=True, config=True)
    login_command = next(
        command
        for command in reversed(controller.commands)
        if command[0] == "create" and command[-2:] == ["login", "openai"]
    )
    mounts = [
        login_command[index + 1]
        for index, item in enumerate(login_command)
        if item == "--mount"
    ]
    assert (
        f"type=bind,src={gateway.config_dir},"
        "dst=/etc/cyclo-gateway,readonly"
    ) in mounts


def test_gateway_usage_never_creates_an_absent_credential_store(
    tmp_path: Path,
) -> None:
    controller = Mock()
    controller.inspect.return_value = None
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )

    with pytest.raises(CycloError, match="credential store is absent"):
        gateway.usage()

    controller.inspect.assert_called_once_with(
        "volume",
        gateway.store_volume,
    )
    controller.call.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    (
        KeyboardInterrupt(),
        CycloError("login failed"),
    ),
)
def test_gateway_tool_failure_removes_the_exact_container(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    controller = Mock()
    controller.require_image.return_value = IMAGE_ID
    controller.call.side_effect = [
        subprocess.CompletedProcess(
            ["create"],
            0,
            f"{CONTAINER_ID}\n",
            "",
        ),
        failure,
    ]
    controller.stop.return_value = True
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )

    with pytest.raises(type(failure)) as error:
        gateway._tool(
            ["login", "openai-codex"],
            volume=False,
            interactive=True,
            capture=False,
        )

    assert error.value is failure
    commands = [
        call.args[0]
        for call in controller.call.call_args_list
    ]
    create = commands[0]
    removed, identifier = controller.stop.call_args.args
    assert create[0] == "create"
    assert create[1] == "--name"
    assert create[2].startswith(f"{gateway.component.container}-tool-")
    labels = {
        create[index + 1]
        for index, item in enumerate(create)
        if item == "--label"
    }
    assert labels == {
        f"{key}={value}"
        for key, value in {
            **ComponentController.expected_labels(removed),
            LABEL_TOOL: "1",
        }.items()
    }
    assert "--interactive" in create
    assert "--rm" not in create
    assert commands[1] == [
        "start",
        "--attach",
        "--interactive",
        CONTAINER_ID,
    ]
    assert removed.container == create[2]
    assert identifier == CONTAINER_ID
    assert removed.preserve_volumes


@pytest.mark.parametrize(
    ("create_result", "error_type"),
    (
        (KeyboardInterrupt(), KeyboardInterrupt),
        (
            subprocess.CompletedProcess(["create"], 0, "invalid\n", ""),
            CycloError,
        ),
    ),
)
def test_gateway_tool_create_failure_cleans_up_by_owned_name(
    tmp_path: Path,
    create_result: BaseException | subprocess.CompletedProcess[str],
    error_type: type[BaseException],
) -> None:
    controller = Mock()
    controller.require_image.return_value = IMAGE_ID
    if isinstance(create_result, BaseException):
        controller.call.side_effect = create_result
    else:
        controller.call.return_value = create_result
    controller.stop.return_value = False
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )

    with pytest.raises(error_type):
        gateway._tool(["providers"], volume=False)

    removed, identifier = controller.stop.call_args.args
    assert removed.container.startswith(
        f"{gateway.component.container}-tool-"
    )
    assert identifier is None
    assert removed.preserve_volumes


def test_gateway_store_guard_removes_a_volume_free_abandoned_tool(
    tmp_path: Path,
) -> None:
    controller = Mock()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )
    tool = replace(
        gateway.component,
        container=f"{gateway.component.container}-tool-{'c' * 32}",
    )
    info = {
        "Id": CONTAINER_ID,
        "Name": f"/{tool.container}",
        "Config": {
            "Labels": {
                **ComponentController.expected_labels(tool),
                LABEL_TOOL: "1",
            }
        },
    }
    controller.call.side_effect = [
        subprocess.CompletedProcess(
            ["container", "ls", "tools"],
            0,
            f"{CONTAINER_ID}\n",
            "",
        ),
        subprocess.CompletedProcess(
            ["container", "ls", "volume"],
            0,
            "",
            "",
        ),
    ]
    controller.inspect.return_value = info
    controller.container_id.side_effect = ComponentController.container_id
    controller.require_owned.side_effect = (
        ComponentController().require_owned
    )
    controller.stop.return_value = True

    gateway._require_exclusive_store()

    controller.stop.assert_called_once_with(tool, CONTAINER_ID)


def test_gateway_never_adopts_a_malformed_labeled_tool(
    tmp_path: Path,
) -> None:
    controller = Mock()
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )
    info = {
        "Id": CONTAINER_ID,
        "Name": "/foreign-container",
        "Config": {
            "Labels": {
                **ComponentController.expected_labels(gateway.component),
                LABEL_TOOL: "1",
            }
        },
    }
    controller.call.return_value = subprocess.CompletedProcess(
        ["container", "ls"],
        0,
        f"{CONTAINER_ID}\n",
        "",
    )
    controller.inspect.return_value = info
    controller.container_id.side_effect = ComponentController.container_id

    with pytest.raises(CycloError, match="malformed gateway tool"):
        gateway._require_exclusive_store()

    controller.stop.assert_not_called()


def test_gateway_tool_cleanup_failure_preserves_both_errors(
    tmp_path: Path,
) -> None:
    controller = Mock()
    controller.require_image.return_value = IMAGE_ID
    controller.call.side_effect = [
        subprocess.CompletedProcess(
            ["create"],
            0,
            f"{CONTAINER_ID}\n",
            "",
        ),
        CycloError("login failed"),
    ]
    controller.stop.side_effect = CycloError("Docker removal failed")
    gateway = Gateway(
        tmp_path / "components",
        controller=controller,
    )

    with pytest.raises(
        CycloError,
        match="login failed; gateway tool cleanup failed: Docker removal failed",
    ) as error:
        gateway._tool(["login", "openai"], volume=False)

    assert isinstance(error.value.__cause__, CycloError)
    assert str(error.value.__cause__) == "login failed"


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
        ("tool", ("login", "anthropic"), True),
        "restart",
    ]


@pytest.mark.parametrize(
    "failure",
    (
        CycloError("candidate catalogue is invalid"),
        KeyboardInterrupt(),
    ),
)
def test_gateway_unsuccessful_login_does_not_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
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
        Mock(side_effect=failure),
    )
    with pytest.raises(type(failure)) as error:
        gateway.login(["unknown", "--api-key-stdin"])

    assert error.value is failure
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
            if any(
                item == f"label={LABEL_TOOL}=1"
                for item in arguments
            ):
                return subprocess.CompletedProcess(arguments, 0, "", "")
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
