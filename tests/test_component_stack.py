from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cyclo.component_stack as component_stack
from cyclo.component_stack import (
    COMPONENT_INTERFACE,
    PROVIDER_INTERFACE,
    ComponentDocker,
    Deployment,
    DockerStatus,
    Gateway,
    Mount,
    ProviderStack,
    component_ready,
    connect_unary,
    load_assembly,
)
from cyclo.errors import CycloError


IMAGE_ID = f"sha256:{'a' * 64}"
CONTAINER_ID = "b" * 64


def _write_component(
    root: Path,
    directory_name: str,
    *,
    component_name: str = "passthrough",
    requirements: tuple[tuple[str, str], ...] = (("upstream", PROVIDER_INTERFACE),),
) -> Path:
    source = root / directory_name
    source.mkdir(parents=True)
    (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    declaration = [
        f"component {component_name}",
        f"provide {COMPONENT_INTERFACE}",
        f"provide {PROVIDER_INTERFACE}",
        *(f"require {name} {service}" for name, service in requirements),
        "",
    ]
    (source / "component.conf").write_text("\n".join(declaration), encoding="utf-8")
    return source


def test_host_assembly_is_ordered_and_gateway_is_the_only_root(tmp_path: Path) -> None:
    first = _write_component(tmp_path, "first")
    second = _write_component(tmp_path, "second")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first context=.. upstream=gateway -- mode=plain\n"
        "provider second ./second upstream=first\n",
        encoding="utf-8",
    )

    assembly = load_assembly(config)

    assert [provider.instance for provider in assembly.providers] == ["first", "second"]
    assert assembly.providers[0].source == first
    assert assembly.providers[0].build_context == tmp_path
    assert assembly.providers[0].arguments == ("mode=plain",)
    assert assembly.providers[1].source == second
    assert assembly.providers[1].bindings == (("upstream", "first"),)

    config.write_text("provider first ./first upstream=later\n", encoding="utf-8")
    with pytest.raises(CycloError, match="unknown or later provider"):
        load_assembly(config)

    root = _write_component(tmp_path, "root", requirements=())
    assert root.is_dir()
    config.write_text("provider root ./root\n", encoding="utf-8")
    with pytest.raises(CycloError, match="must require an upstream"):
        load_assembly(config)


def test_host_assembly_rejects_symlinked_component_files(tmp_path: Path) -> None:
    source = _write_component(tmp_path, "source")
    declaration = source / "component.conf"
    real = source / "real.conf"
    declaration.rename(real)
    declaration.symlink_to(real.name)
    config = tmp_path / "host.conf"
    config.write_text("provider pass ./source upstream=gateway\n", encoding="utf-8")

    with pytest.raises(CycloError, match="not a regular file"):
        load_assembly(config)


def _fake_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
    body: bytes,
) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []

    class Response:
        reason = "test response"

        def __init__(self) -> None:
            self.status = status

        def read(self, limit: int) -> bytes:
            return body[:limit]

    class Connection:
        def __init__(self, socket_path: Path, timeout: float) -> None:
            self.socket_path = socket_path
            self.timeout = timeout

        def request(self, method: str, path: str, *, body: bytes, headers) -> None:
            requests.append(
                {
                    "socket_path": self.socket_path,
                    "timeout": self.timeout,
                    "method": method,
                    "path": path,
                    "headers": dict(headers),
                    "body": body,
                }
            )

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(component_stack, "_UnixHTTPConnection", Connection)
    return requests


def test_connect_unary_speaks_connect_json_over_a_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "component.sock"
    payload = json.dumps(
        {"status": "HEALTH_STATUS_READY", "message": "ready"}
    ).encode("utf-8")
    requests = _fake_connection(monkeypatch, status=200, body=payload)

    assert component_ready(socket_path)

    assert len(requests) == 1
    assert requests[0]["socket_path"] == socket_path
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == f"/{COMPONENT_INTERFACE}/Health"
    assert requests[0]["body"] == b"{}"
    headers = requests[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Content-Type"] == "application/json"
    assert headers["Connect-Protocol-Version"] == "1"
    assert headers["Content-Length"] == "2"


@pytest.mark.parametrize(
    ("socket_path", "service", "method", "timeout", "message"),
    [
        (Path("relative.sock"), COMPONENT_INTERFACE, "Health", 1, "must be absolute"),
        (Path("/tmp/component.sock"), "bad/service", "Health", 1, "service name"),
        (Path("/tmp/component.sock"), COMPONENT_INTERFACE, "Bad/Method", 1, "method name"),
        (Path("/tmp/component.sock"), COMPONENT_INTERFACE, "Health", 0, "timeout"),
        (Path("/tmp/component.sock"), COMPONENT_INTERFACE, "Health", float("inf"), "timeout"),
    ],
)
def test_connect_unary_rejects_invalid_calls_before_connecting(
    socket_path: Path,
    service: str,
    method: str,
    timeout: float,
    message: str,
) -> None:
    with pytest.raises(CycloError, match=message):
        connect_unary(socket_path, service, method, timeout=timeout)


def test_connect_unary_fails_closed_on_errors_and_oversized_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "component.sock"
    _fake_connection(monkeypatch, status=503, body=b'{"message":"not ready"}')
    with pytest.raises(CycloError, match=r"failed \(503\): not ready"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")

    _fake_connection(monkeypatch, status=200, body=b"[]")
    with pytest.raises(CycloError, match="not a JSON object"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")

    _fake_connection(monkeypatch, status=200, body=b'{"status":NaN}')
    with pytest.raises(CycloError, match="invalid JSON"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")

    monkeypatch.setattr(component_stack, "MAX_RPC_BYTES", 16)
    _fake_connection(
        monkeypatch,
        status=200,
        body=b'{"message":"far too large"}',
    )
    with pytest.raises(CycloError, match="response exceeds"):
        connect_unary(socket_path, COMPONENT_INTERFACE, "Health")
    with pytest.raises(CycloError, match="request exceeds"):
        connect_unary(
            socket_path,
            COMPONENT_INTERFACE,
            "Health",
            {"message": "far too large"},
        )
    with pytest.raises(CycloError, match="request is not valid JSON"):
        connect_unary(
            socket_path,
            COMPONENT_INTERFACE,
            "Health",
            {"temperature": float("nan")},
        )


def _deployment() -> Deployment:
    return Deployment(
        instance="pass",
        component_type="passthrough",
        source=Path("/component/pass"),
        build_context=Path("/component"),
        image="cyclo-0123456789ab-pass:latest",
        container="cyclo-0123456789ab-pass",
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
    )


def _labels() -> dict[str, str]:
    return ComponentDocker._expected_labels(_deployment())


def _image() -> dict[str, object]:
    return {
        "Id": IMAGE_ID,
        "Config": {
            "Labels": _labels(),
            "Entrypoint": ["node", "src/main.mjs"],
            "User": "1000:1000",
            "Healthcheck": {"Test": ["CMD", "node", "src/healthcheck.mjs"]},
            "Env": ["PATH=/usr/bin"],
            "WorkingDir": "/component",
            "ExposedPorts": {},
            "Volumes": None,
        },
    }


def _container() -> dict[str, object]:
    image = _image()["Config"]
    assert isinstance(image, dict)
    return {
        "Id": CONTAINER_ID,
        "Name": f"/{_deployment().container}",
        "Image": IMAGE_ID,
        "Config": {
            "Labels": _labels(),
            "Cmd": ["serve", "mode=plain"],
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
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864"},
            "PortBindings": {},
        },
        "Mounts": [
            {
                "Type": mount.type,
                "Source": mount.source,
                "Destination": mount.destination,
                "RW": not mount.read_only,
            }
            for mount in _deployment().mounts
        ],
        "State": {
            "Running": True,
            "Status": "running",
            "Health": {"Status": "healthy"},
        },
        "NetworkSettings": {"Networks": {"none": {}}, "Ports": {}},
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("HostConfig", "UTSMode"), "host"),
        (("HostConfig", "UsernsMode"), "host"),
        (("HostConfig", "CapAdd"), ["SYS_ADMIN"]),
        (("HostConfig", "Devices"), [{"PathOnHost": "/dev/kvm"}]),
        (("HostConfig", "DeviceRequests"), [{"Driver": "nvidia"}]),
        (("HostConfig", "Tmpfs"), {"/tmp": "rw", "/run": "rw"}),
        (("HostConfig", "PortBindings"), {"8080/tcp": [{"HostPort": "1"}]}),
        (("NetworkSettings", "Ports"), {"8080/tcp": [{"HostPort": "1"}]}),
    ],
)
def test_container_currentness_includes_every_isolation_boundary(
    path: tuple[str, str], value: object
) -> None:
    docker = ComponentDocker()
    image = _image()
    container = _container()
    assert docker._configuration_current(_deployment(), image, container)

    changed = copy.deepcopy(container)
    section = changed[path[0]]
    assert isinstance(section, dict)
    section[path[1]] = value
    assert not docker._configuration_current(_deployment(), image, changed)


def test_status_requires_the_official_image_id_and_exact_ownership_labels() -> None:
    class Docker(ComponentDocker):
        def __init__(self, image: dict[str, object], container: dict[str, object]) -> None:
            self.image = image
            self.container = container

        def inspect(self, kind: str, _reference: str, *, missing: bool = True):
            return self.image if kind == "image" else self.container

    assert Docker(_image(), _container()).status(_deployment()).current

    stale = _container()
    stale["Image"] = f"sha256:{'d' * 64}"
    assert not Docker(_image(), stale).status(_deployment()).current

    wrong_type = _image()
    wrong_type_config = wrong_type["Config"]
    assert isinstance(wrong_type_config, dict)
    wrong_type_labels = wrong_type_config["Labels"]
    assert isinstance(wrong_type_labels, dict)
    wrong_type_labels[component_stack.LABEL_TYPE] = "another-type"
    assert not Docker(wrong_type, _container()).status(_deployment()).current

    foreign = _image()
    foreign_config = foreign["Config"]
    assert isinstance(foreign_config, dict)
    foreign_labels = foreign_config["Labels"]
    assert isinstance(foreign_labels, dict)
    foreign_labels[component_stack.LABEL_SYSTEM] = "foreign"
    with pytest.raises(CycloError, match="not owned"):
        Docker(foreign, _container()).status(_deployment())


@pytest.mark.parametrize("valid", [True, False])
def test_build_promotes_only_a_completed_image_that_satisfies_the_contract(
    valid: bool,
) -> None:
    class Docker(ComponentDocker):
        def __init__(self) -> None:
            self.candidate: str | None = None
            self.official = False
            self.events: list[list[str]] = []

        def inspect(self, kind: str, reference: str, *, missing: bool = True):
            assert kind == "image"
            if reference == _deployment().image and not self.official:
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
                self.candidate = command[command.index("--tag") + 1]
                Path(command[command.index("--iidfile") + 1]).write_text(
                    f"{IMAGE_ID}\n",
                    encoding="utf-8",
                )
            if command[:2] == ["image", "tag"]:
                self.official = True
            return subprocess.CompletedProcess(command, 0, "", "")

    docker = Docker()
    if valid:
        assert docker.build(_deployment()) == IMAGE_ID
        assert docker.official
    else:
        with pytest.raises(CycloError, match="HEALTHCHECK"):
            docker.build(_deployment())
        assert not docker.official
    assert docker.events[-1][:2] == ["image", "rm"]


def test_start_rolls_back_the_exact_owned_container_even_on_keyboard_interrupt() -> None:
    class Docker(ComponentDocker):
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.inspect_count = 0

        def inspect(self, kind: str, reference: str, *, missing: bool = True):
            assert kind == "container"
            self.inspect_count += 1
            if self.inspect_count == 1:
                return None
            return {
                "Id": CONTAINER_ID,
                "Name": f"/{_deployment().container}",
                "Config": {"Labels": _labels()},
            }

        def require_image(self, deployment: Deployment) -> str:
            return IMAGE_ID

        def call(self, arguments, **_kwargs):
            self.calls.append(list(arguments))
            if arguments[0] == "run":
                return subprocess.CompletedProcess(arguments, 0, f"{CONTAINER_ID}\n", "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def status(self, deployment: Deployment) -> DockerStatus:
            raise KeyboardInterrupt

    docker = Docker()
    with pytest.raises(KeyboardInterrupt):
        docker.start(_deployment())
    assert ["rm", "--force", "--volumes", CONTAINER_ID] in docker.calls


def test_provider_stop_is_reverse_order_then_owned_stray_cleanup(tmp_path: Path) -> None:
    _write_component(tmp_path, "first")
    _write_component(tmp_path, "second")
    config = tmp_path / "host.conf"
    config.write_text(
        "provider first ./first upstream=gateway\n"
        "provider second ./second upstream=first\n",
        encoding="utf-8",
    )

    class Docker:
        def __init__(self) -> None:
            self.events: list[tuple[object, ...]] = []

        def stop(self, deployment: Deployment) -> bool:
            self.events.append(("stop", deployment.instance))
            return True

        def stop_system(self, system: str, **options: object) -> tuple[str, ...]:
            self.events.append(("stop_system", system, options))
            return ("orphan",)

    docker = Docker()
    gateway = SimpleNamespace(
        socket_dir=tmp_path / "gateway",
        socket_path=tmp_path / "gateway" / "component.sock",
    )
    stack = ProviderStack(
        tmp_path / "state",
        config,
        gateway=gateway,
        docker=docker,  # type: ignore[arg-type]
    )

    assert stack.stop() == ("second", "first", "orphan")
    assert docker.events[0:2] == [("stop", "second"), ("stop", "first")]
    assert docker.events[2][2] == {
        "lifecycle": "provider",
    }


def test_gateway_usage_mounts_the_credential_volume_read_only(tmp_path: Path) -> None:
    class Docker:
        def __init__(self) -> None:
            self.gateway: Gateway | None = None
            self.commands: list[list[str]] = []

        def require_image(self, _deployment: Deployment) -> str:
            return IMAGE_ID

        def inspect(self, kind: str, _reference: str, **_options: object):
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

    docker = Docker()
    gateway = Gateway(tmp_path / "state", docker=docker)  # type: ignore[arg-type]
    docker.gateway = gateway

    assert gateway.usage() == {"totals": {"requests": 0}}
    mount = docker.commands[-1][docker.commands[-1].index("--mount") + 1]
    assert mount.endswith("/var/lib/cyclo-gateway,readonly")


def test_gateway_login_rejects_a_running_stale_container_before_store_use(
    tmp_path: Path,
) -> None:
    class Docker:
        def status(self, _deployment: Deployment) -> DockerStatus:
            return DockerStatus(
                IMAGE_ID,
                CONTAINER_ID,
                True,
                "running",
                "healthy",
                False,
            )

    gateway = Gateway(tmp_path / "state", docker=Docker())  # type: ignore[arg-type]
    with pytest.raises(CycloError, match="running gateway is stale"):
        gateway.login(["anthropic"])


def test_destroy_store_checks_for_foreign_users_before_stopping_gateway(
    tmp_path: Path,
) -> None:
    foreign_id = "c" * 64

    class Docker:
        def __init__(self) -> None:
            self.gateway: Gateway | None = None
            self.stopped = False

        def inspect(self, kind: str, _reference: str, **_options: object):
            assert kind == "volume"
            assert self.gateway is not None
            return {
                "Name": self.gateway.store_volume,
                "Driver": "local",
                "Scope": "local",
                "Labels": self.gateway._volume_labels(),
                "Options": {},
            }

        def status(self, _deployment: Deployment) -> DockerStatus:
            return DockerStatus(
                IMAGE_ID,
                CONTAINER_ID,
                True,
                "running",
                "healthy",
                True,
            )

        def call(self, arguments, **_options):
            assert arguments[:2] == ["container", "ls"]
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"{CONTAINER_ID}\n{foreign_id}\n",
                "",
            )

        def stop(self, _deployment: Deployment) -> bool:
            self.stopped = True
            return True

    docker = Docker()
    gateway = Gateway(tmp_path / "state", docker=docker)  # type: ignore[arg-type]
    docker.gateway = gateway

    with pytest.raises(CycloError, match="mounted by another container"):
        gateway.destroy_store()
    assert not docker.stopped
