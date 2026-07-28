from __future__ import annotations

import copy
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from cyclo import __version__
import cyclo.component_runtime as runtime_module
from cyclo.component import (
    COMPONENT_INTERFACE,
    Component,
    ComponentStatus,
    Declaration,
    Mount,
)
from cyclo.component_runtime import (
    LABEL_RELEASE,
    LABEL_TYPE,
    ComponentController,
)
from cyclo.errors import CycloError
from cyclo.installation import LABEL_SYSTEM


IMAGE_ID = f"sha256:{'a' * 64}"
CONTAINER_ID = "b" * 64


def _component() -> Component:
    return Component(
        name="pass",
        declaration=Declaration(
            "passthrough",
            (COMPONENT_INTERFACE, "cyclo.provider.v1.Provider"),
            (),
        ),
        source=Path("/component/pass"),
        build_context=Path("/component"),
        image="cyclo-0123456789ab-provider-pass:latest",
        container="cyclo-0123456789ab-provider-pass",
        system="0123456789ab",
        arguments=("mode=plain",),
        mounts=(
            Mount("/state/pass", "/run/cyclo"),
            Mount(
                "/state/gateway",
                "/run/cyclo/requirements/upstream",
                read_only=True,
            ),
        ),
        network="none",
        socket_path=Path("/state/pass/component.sock"),
    )


def _labels() -> dict[str, str]:
    return ComponentController.expected_labels(_component())


def _image() -> dict[str, object]:
    return {
        "Id": IMAGE_ID,
        "Config": {
            "Labels": _labels(),
            "Entrypoint": ["node", "src/main.mjs"],
            "User": "1000:1000",
            "Healthcheck": {
                "Test": ["CMD", "node", "src/healthcheck.mjs"]
            },
            "Env": ["PATH=/usr/bin"],
            "WorkingDir": "/component",
            "Cmd": ["image-default"],
            "ExposedPorts": {},
            "Volumes": None,
        },
    }


def _container() -> dict[str, object]:
    image = _image()["Config"]
    assert isinstance(image, dict)
    return {
        "Id": CONTAINER_ID,
        "Name": f"/{_component().container}",
        "Image": IMAGE_ID,
        "Config": {
            "Labels": _labels(),
            "Cmd": ["mode=plain"],
            "User": image["User"],
            "Entrypoint": image["Entrypoint"],
            "Healthcheck": image["Healthcheck"],
            "Env": image["Env"],
            "WorkingDir": image["WorkingDir"],
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "PidsLimit": 256,
            "RestartPolicy": {"Name": "unless-stopped"},
            "SecurityOpt": ["no-new-privileges"],
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "Devices": [],
            "DeviceRequests": [],
            "Ulimits": [
                {"Name": "nofile", "Soft": 1024, "Hard": 1024}
            ],
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=67108864"
            },
            "PortBindings": {},
        },
        "Mounts": [
            {
                "Type": mount.type,
                "Source": mount.source,
                "Destination": mount.destination,
                "RW": not mount.read_only,
            }
            for mount in _component().mounts
        ],
        "State": {
            "Running": True,
            "Status": "running",
            "Health": {"Status": "healthy"},
        },
        "NetworkSettings": {"Networks": {"none": {}}, "Ports": {}},
    }


def _ready_status(component: Component) -> ComponentStatus:
    return ComponentStatus(
        component.name,
        component.kind,
        IMAGE_ID,
        CONTAINER_ID,
        True,
        "running",
        "healthy",
        True,
        "ready",
    )


class _ImageController(ComponentController):
    def __init__(self, image: dict[str, object] | None) -> None:
        self.image = image
        self.builds: list[Component] = []

    def inspect(
        self,
        kind: str,
        reference: str,
        *,
        missing: bool = True,
    ):
        assert kind == "image"
        assert reference == _component().image
        return self.image

    def build(self, component: Component) -> str:
        self.builds.append(component)
        return IMAGE_ID


class _LifecycleController(ComponentController):
    def __init__(self, status: ComponentStatus) -> None:
        self.observed = status
        self.events: list[str] = []

    def status(
        self,
        component: Component,
        *,
        error: str = "",
    ) -> ComponentStatus:
        self.events.append("status")
        return self.observed

    def ensure_image(self, component: Component) -> str:
        self.events.append("ensure")
        return IMAGE_ID

    def require_image(self, component: Component) -> str:
        self.events.append("require")
        return IMAGE_ID

    def build(self, component: Component) -> str:
        self.events.append("build")
        return IMAGE_ID

    def start_built(
        self,
        component: Component,
        *,
        replace: bool = False,
    ) -> ComponentStatus:
        self.events.append(f"start:{replace}")
        return _ready_status(component)


def test_component_images_are_scoped_to_the_cyclo_release() -> None:
    assert _labels()[LABEL_RELEASE] == __version__


def test_current_image_check_reports_a_release_mismatch() -> None:
    image = _image()
    config = image["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels[LABEL_RELEASE] = "another-release"

    with pytest.raises(CycloError, match="different Cyclo release"):
        _ImageController(image).require_image(_component())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("HostConfig", "UTSMode"), "host"),
        (("HostConfig", "UsernsMode"), "host"),
        (("HostConfig", "CapAdd"), ["SYS_ADMIN"]),
        (("HostConfig", "Devices"), [{"PathOnHost": "/dev/kvm"}]),
        (("HostConfig", "DeviceRequests"), [{"Driver": "nvidia"}]),
        (("HostConfig", "Tmpfs"), {"/tmp": "rw", "/run": "rw"}),
        (
            ("HostConfig", "PortBindings"),
            {"8080/tcp": [{"HostPort": "1"}]},
        ),
        (
            ("NetworkSettings", "Ports"),
            {"8080/tcp": [{"HostPort": "1"}]},
        ),
    ],
)
def test_container_currentness_includes_every_isolation_boundary(
    path: tuple[str, str],
    value: object,
) -> None:
    controller = ComponentController()
    image = _image()
    container = _container()
    assert controller._configuration_current(
        _component(),
        image,
        container,
    )

    changed = copy.deepcopy(container)
    section = changed[path[0]]
    assert isinstance(section, dict)
    section[path[1]] = value
    assert not controller._configuration_current(
        _component(),
        image,
        changed,
    )


def test_container_command_is_default_or_an_explicit_override() -> None:
    controller = ComponentController()
    image = _image()

    default_component = replace(_component(), arguments=())
    default_container = _container()
    default_config = default_container["Config"]
    assert isinstance(default_config, dict)
    default_config["Cmd"] = ["image-default"]
    assert controller._configuration_current(
        default_component,
        image,
        default_container,
    )

    assert controller._configuration_current(
        _component(),
        image,
        _container(),
    )

    hidden_convention = _container()
    hidden_config = hidden_convention["Config"]
    assert isinstance(hidden_config, dict)
    hidden_config["Cmd"] = ["serve", "mode=plain"]
    assert not controller._configuration_current(
        _component(),
        image,
        hidden_convention,
    )


def test_status_requires_official_image_and_exact_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "probe_component",
        lambda _path: ("ready", ""),
    )

    class Controller(ComponentController):
        def __init__(
            self,
            image: dict[str, object],
            container: dict[str, object],
        ) -> None:
            self.selected_image = image
            self.selected_container = container

        def inspect(
            self,
            kind: str,
            _reference: str,
            *,
            missing: bool = True,
        ):
            return (
                self.selected_image
                if kind == "image"
                else self.selected_container
            )

    assert Controller(_image(), _container()).status(_component()).works

    stale = _container()
    stale["Image"] = f"sha256:{'d' * 64}"
    assert not Controller(_image(), stale).status(_component()).current

    wrong_type = _image()
    config = wrong_type["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels[LABEL_TYPE] = "another-type"
    wrong_type_status = Controller(
        wrong_type,
        _container(),
    ).status(_component())
    assert not wrong_type_status.current
    assert wrong_type_status.error == (
        "Docker image has incomplete component labels: "
        f"{_component().image}"
    )

    invalid_contract = _image()
    invalid_config = invalid_contract["Config"]
    assert isinstance(invalid_config, dict)
    invalid_config["Entrypoint"] = None
    invalid_status = Controller(
        invalid_contract,
        _container(),
    ).status(_component())
    assert not invalid_status.current
    assert invalid_status.error == (
        "component image must define OCI ENTRYPOINT"
    )

    foreign = _image()
    config = foreign["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels[LABEL_SYSTEM] = "foreign"
    with pytest.raises(CycloError, match="not owned"):
        Controller(foreign, _container()).status(_component())


def test_startup_status_and_logs_stay_bound_to_the_expected_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references: list[tuple[str, str]] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(
        runtime_module,
        "probe_component",
        lambda _path: ("ready", ""),
    )

    class Controller(ComponentController):
        def inspect(
            self,
            kind: str,
            reference: str,
            *,
            missing: bool = True,
        ):
            references.append((kind, reference))
            if kind == "image":
                return _image()
            assert kind == "container"
            assert reference == CONTAINER_ID
            return _container()

        def call(self, arguments, **_options):
            commands.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, "", "")

    controller = Controller()
    assert controller.status(
        _component(),
        expected_id=CONTAINER_ID,
    ).works
    assert controller.logs(
        _component(),
        expected_id=CONTAINER_ID,
    ) == ""
    assert references == [
        ("image", _component().image),
        ("container", CONTAINER_ID),
        ("container", CONTAINER_ID),
    ]
    assert commands == [["logs", "--tail", "80", CONTAINER_ID]]


def test_wait_ready_uses_the_expected_id_for_status_and_diagnostics() -> None:
    observed: list[tuple[str, str | None]] = []

    class Controller(ComponentController):
        def status(
            self,
            component: Component,
            *,
            error: str = "",
            expected_id: str | None = None,
        ) -> ComponentStatus:
            observed.append(("status", expected_id))
            return _ready_status(component)

        def logs(
            self,
            component: Component,
            lines: int = 80,
            *,
            expected_id: str | None = None,
        ) -> str:
            observed.append(("logs", expected_id))
            return ""

    status = Controller().wait_ready(_component(), CONTAINER_ID)
    assert status.works
    assert observed == [
        ("status", CONTAINER_ID),
        ("status", CONTAINER_ID),
    ]


@pytest.mark.parametrize("valid", [True, False])
def test_build_promotes_only_a_completed_valid_image(valid: bool) -> None:
    class Controller(ComponentController):
        def __init__(self) -> None:
            self.official = False
            self.events: list[list[str]] = []

        def inspect(
            self,
            kind: str,
            reference: str,
            *,
            missing: bool = True,
        ):
            assert kind == "image"
            if reference == _component().image and not self.official:
                return None
            inspected = _image()
            if not valid:
                config = inspected["Config"]
                assert isinstance(config, dict)
                config.pop("Healthcheck")
            return inspected

        def call(self, arguments, **_options):
            command = list(arguments)
            self.events.append(command)
            if command[0] == "build":
                Path(command[command.index("--iidfile") + 1]).write_text(
                    f"{IMAGE_ID}\n",
                    encoding="utf-8",
                )
            if command[:2] == ["image", "tag"]:
                self.official = True
            return subprocess.CompletedProcess(command, 0, "", "")

    controller = Controller()
    if valid:
        assert controller.build(_component()) == IMAGE_ID
        assert controller.official
    else:
        with pytest.raises(CycloError, match="HEALTHCHECK"):
            controller.build(_component())
        assert not controller.official
    build = controller.events[0]
    assert build[0] == "build"
    assert "--iidfile" in build
    assert "--tag" not in build
    assert not any(event[:2] == ["image", "rm"] for event in controller.events)


def test_interrupted_create_never_cleans_up_by_a_reusable_name() -> None:
    class Controller(ComponentController):
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.inspect_count = 0

        def inspect(
            self,
            kind: str,
            reference: str,
            *,
            missing: bool = True,
        ):
            assert kind == "container"
            self.inspect_count += 1
            if self.inspect_count == 1:
                return None
            return {
                "Id": CONTAINER_ID,
                "Name": f"/{_component().container}",
                "Config": {"Labels": _labels()},
                "State": {"Running": False, "Status": "created"},
            }

        def require_image(self, component: Component) -> str:
            assert component == _component()
            return IMAGE_ID

        def call(self, arguments, **_kwargs):
            self.calls.append(list(arguments))
            if arguments[0] == "create":
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def status(
            self,
            component: Component,
            *,
            error: str = "",
        ) -> ComponentStatus:
            pytest.fail("an interrupted create must never start the container")

    controller = Controller()
    with pytest.raises(KeyboardInterrupt):
        controller._run(_component())
    assert len(controller.calls) == 1
    assert controller.calls[0][:3] == [
        "create",
        "--name",
        _component().container,
    ]
    assert controller.inspect_count == 1
    assert not any(command[0] == "start" for command in controller.calls)
    assert not any(command[0] == "rm" for command in controller.calls)


class _LegacyRuntimeError(RuntimeError):
    """Model an exception without BaseException.add_note, as on Python 3.10."""

    add_note = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    "launch_error",
    (
        CycloError("launch inspection failed"),
        _LegacyRuntimeError("launch inspection failed"),
    ),
)
def test_failed_start_reports_an_incomplete_container_rollback(
    launch_error: Exception,
) -> None:
    class Controller(ComponentController):
        def __init__(self) -> None:
            self.inspect_count = 0

        def inspect(
            self,
            kind: str,
            _reference: str,
            *,
            missing: bool = True,
        ):
            assert kind == "container"
            self.inspect_count += 1
            if self.inspect_count == 1:
                return None
            return {
                "Id": CONTAINER_ID,
                "Name": f"/{_component().container}",
                "Config": {"Labels": _labels()},
                "State": {"Running": False, "Status": "created"},
            }

        def require_image(self, _component: Component) -> str:
            return IMAGE_ID

        def call(self, arguments, **_options):
            if arguments[0] == "create":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    f"{CONTAINER_ID}\n",
                    "",
                )
            if arguments[0] == "start":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            raise CycloError("Docker refused cleanup")

        def status(
            self,
            _component: Component,
            *,
            error: str = "",
            expected_id: str | None = None,
        ) -> ComponentStatus:
            assert expected_id == CONTAINER_ID
            raise launch_error

    with pytest.raises(
        CycloError,
        match="launch inspection failed.*rollback failed.*refused cleanup",
    ):
        Controller()._run(_component())


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            {"Running": True, "Paused": True, "Status": "paused"},
            [
                ["unpause", CONTAINER_ID],
                ["stop", "--timeout", "10", CONTAINER_ID],
                ["rm", "--volumes", CONTAINER_ID],
            ],
        ),
        (
            {"Running": False, "Dead": True, "Status": "dead"},
            [["rm", "--force", "--volumes", CONTAINER_ID]],
        ),
    ],
)
def test_stop_handles_paused_and_dead_component_containers(
    state: dict[str, object],
    expected: list[list[str]],
) -> None:
    container = _container()
    container["State"] = state

    class Controller(ComponentController):
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def inspect(
            self,
            kind: str,
            reference: str,
            *,
            missing: bool = True,
        ):
            assert kind == "container"
            assert reference in {_component().container, CONTAINER_ID}
            return container

        def call(self, arguments, **_options):
            self.calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, "", "")

    controller = Controller()
    assert controller.stop(_component())
    assert controller.calls == expected


@pytest.mark.parametrize(
    ("arguments", "expected_command"),
    [
        ((), []),
        (("mode=plain",), ["mode=plain"]),
    ],
)
def test_run_uses_image_command_unless_explicitly_overridden(
    arguments: tuple[str, ...],
    expected_command: list[str],
) -> None:
    component = replace(_component(), arguments=arguments)

    class Controller(ComponentController):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []
            self.inspect_count = 0

        def inspect(
            self,
            kind: str,
            _reference: str,
            *,
            missing: bool = True,
        ):
            assert kind == "container"
            self.inspect_count += 1
            if self.inspect_count == 1:
                return None
            created = _container()
            created["State"] = {"Running": False, "Status": "created"}
            return created

        def require_image(self, selected: Component) -> str:
            assert selected is component
            return IMAGE_ID

        def call(self, arguments, **_options):
            command = list(arguments)
            self.commands.append(command)
            output = f"{CONTAINER_ID}\n" if command[0] == "create" else ""
            return subprocess.CompletedProcess(
                command,
                0,
                output,
                "",
            )

        def status(
            self,
            selected: Component,
            *,
            error: str = "",
            expected_id: str | None = None,
        ) -> ComponentStatus:
            assert selected is component
            assert expected_id == CONTAINER_ID
            return _ready_status(selected)

    controller = Controller()
    assert controller._run(component) == CONTAINER_ID
    create = controller.commands[0]
    assert create[create.index(IMAGE_ID) :] == [IMAGE_ID, *expected_command]
    assert controller.commands[1] == ["start", CONTAINER_ID]


def test_ensure_image_reuses_a_valid_installed_image() -> None:
    component = _component()
    controller = _ImageController(_image())

    assert controller.ensure_image(component) == IMAGE_ID
    assert controller.builds == []


@pytest.mark.parametrize("stale", [False, True])
def test_ensure_image_builds_an_absent_or_old_release(stale: bool) -> None:
    component = _component()
    image = _image() if stale else None
    if image is not None:
        config = image["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels[LABEL_RELEASE] = "0.1.0"

    controller = _ImageController(image)
    assert controller.ensure_image(component) == IMAGE_ID
    assert controller.builds == [component]


@pytest.mark.parametrize(
    ("foreign", "stale"),
    ((False, False), (False, True), (True, False)),
)
def test_ensure_image_rejects_a_malformed_or_foreign_current_image(
    foreign: bool,
    stale: bool,
) -> None:
    component = _component()
    image = _image()
    config = image["Config"]
    assert isinstance(config, dict)
    if foreign:
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels[LABEL_SYSTEM] = "foreign"
    else:
        config["Entrypoint"] = None
        if stale:
            labels = config["Labels"]
            assert isinstance(labels, dict)
            labels[LABEL_RELEASE] = "0.1.0"

    controller = _ImageController(image)
    message = "not owned" if foreign else "ENTRYPOINT"
    with pytest.raises(CycloError, match=message):
        controller.ensure_image(component)
    assert controller.builds == []


def test_start_reuses_a_working_container_without_building() -> None:
    component = _component()
    controller = _LifecycleController(_ready_status(component))

    assert controller.start(component).works
    assert controller.events == ["status"]


def test_start_repairs_an_unready_component_from_the_installed_image() -> None:
    component = _component()
    controller = _LifecycleController(
        replace(
            _ready_status(component),
            running=False,
            container_state="stopped",
            engine_health="missing",
            health="unreachable",
        )
    )

    assert controller.start(component).works
    assert controller.events == ["status", "ensure", "start:False"]


def test_restart_reuses_the_installed_image_and_refresh_rebuilds() -> None:
    component = _component()
    controller = _LifecycleController(_ready_status(component))

    assert controller.restart(component).works
    assert controller.events == ["require", "start:True"]

    controller.events.clear()
    assert controller.refresh(component).works
    assert controller.events == ["build", "start:True"]
