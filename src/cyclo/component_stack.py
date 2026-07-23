from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from math import isfinite
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .errors import CycloError
from .installation import (
    LABEL_INSTANCE,
    LABEL_SYSTEM,
    gateway_name,
    installation_id,
    provider_name,
)


COMPONENT_INTERFACE = "cyclo.component.v1.Component"
PROVIDER_INTERFACE = "cyclo.provider.v1.Provider"
COMPONENT_SOCKET = "component.sock"
CONTAINER_SOCKET_ROOT = Path("/run/cyclo")
CONTAINER_REQUIREMENT_ROOT = CONTAINER_SOCKET_ROOT / "requirements"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RPC_BYTES = 16 * 1024 * 1024

LABEL_OWNED = "io.cyclo.component"
LABEL_TYPE = "io.cyclo.component-type"
LABEL_LIFECYCLE = "io.cyclo.lifecycle"

_INSTANCE_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SERVICE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def component_sources_root() -> Path:
    """Return Cyclo's package-owned component source root."""

    return Path(str(resources.files("cyclo"))).resolve() / "components"


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CycloError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise CycloError(f"{label} is not a regular file: {path}")
    return path


def _canonical_directory(path: Path, label: str) -> Path:
    selected = path.expanduser()
    try:
        metadata = selected.lstat()
        canonical = selected.resolve(strict=True)
    except OSError as exc:
        raise CycloError(f"{label} does not exist: {selected}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or selected.is_symlink():
        raise CycloError(f"{label} is not a directory: {selected}")
    if "," in str(canonical):
        raise CycloError(f"{label} cannot contain a comma: {canonical}")
    return canonical


def _ensure_directory(path: Path, mode: int) -> Path:
    if path.is_symlink():
        raise CycloError(f"component state path is a symlink: {path}")
    try:
        path.mkdir(parents=True, mode=mode, exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise CycloError(f"component state path is not a directory: {path}")
        os.chmod(path, mode)
    except OSError as exc:
        raise CycloError(f"cannot prepare component state directory {path}: {exc}") from exc
    return path


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


@dataclass(frozen=True)
class Requirement:
    name: str
    service: str


@dataclass(frozen=True)
class Declaration:
    name: str
    provides: tuple[str, ...]
    requires: tuple[Requirement, ...]


def parse_declaration(path: Path) -> Declaration:
    _regular_file(path, "component declaration")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CycloError(f"cannot read component declaration {path}: {exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise CycloError(f"component declaration exceeds {MAX_CONFIG_BYTES} bytes: {path}")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CycloError(f"component declaration is not valid UTF-8: {path}") from exc

    name: str | None = None
    provides: list[str] = []
    requires: list[Requirement] = []
    required_names: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        content = re.sub(r"\s+#.*$", "", raw_line).strip()
        if not content or content.startswith("#"):
            continue
        fields = content.split()
        directive = fields[0]
        if directive == "component":
            if len(fields) != 2:
                raise CycloError(f"{path}:{line_number}: expected: component NAME")
            if name is not None:
                raise CycloError(f"{path}:{line_number}: duplicate component declaration")
            if not _INSTANCE_RE.fullmatch(fields[1]):
                raise CycloError(f"{path}:{line_number}: invalid component name {fields[1]!r}")
            name = fields[1]
        elif directive == "provide":
            if len(fields) != 2 or not _SERVICE_RE.fullmatch(fields[1]):
                raise CycloError(f"{path}:{line_number}: expected: provide SERVICE")
            if fields[1] in provides:
                raise CycloError(f"{path}:{line_number}: duplicate provided interface {fields[1]}")
            provides.append(fields[1])
        elif directive == "require":
            if (
                len(fields) != 3
                or not _INSTANCE_RE.fullmatch(fields[1])
                or not _SERVICE_RE.fullmatch(fields[2])
            ):
                raise CycloError(f"{path}:{line_number}: expected: require NAME SERVICE")
            if fields[1] in required_names:
                raise CycloError(f"{path}:{line_number}: duplicate requirement {fields[1]}")
            required_names.add(fields[1])
            requires.append(Requirement(fields[1], fields[2]))
        else:
            raise CycloError(f"{path}:{line_number}: unknown directive {directive}")
    if name is None:
        raise CycloError(f"{path}:1: missing component declaration")
    if COMPONENT_INTERFACE not in provides:
        raise CycloError(f"{path}:1: every component must provide {COMPONENT_INTERFACE}")
    return Declaration(name, tuple(provides), tuple(requires))


@dataclass(frozen=True)
class ProviderDefinition:
    instance: str
    source: Path
    build_context: Path
    declaration: Declaration
    bindings: tuple[tuple[str, str], ...]
    arguments: tuple[str, ...]
    line: int

    def target(self, requirement: str) -> str:
        for name, target in self.bindings:
            if name == requirement:
                return target
        raise KeyError(requirement)


@dataclass(frozen=True)
class Assembly:
    path: Path
    providers: tuple[ProviderDefinition, ...]
    generation: str


def _strip_comment(line: str) -> str:
    marker = re.search(r"(?:^|\s)#", line)
    return line if marker is None else line[: marker.start()]


def load_assembly(path: Path) -> Assembly:
    selected = path.expanduser()
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    selected = Path(os.path.abspath(selected))
    try:
        raw = selected.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise CycloError(f"{selected}:1: cannot read configuration: {exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise CycloError(f"{selected}:1: configuration exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CycloError(f"{selected}:1: configuration is not valid UTF-8") from exc

    providers: list[ProviderDefinition] = []
    available: dict[str, set[str]] = {
        "gateway": {COMPONENT_INTERFACE, PROVIDER_INTERFACE}
    }
    generation = hashlib.sha256(raw).hexdigest()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        content = _strip_comment(raw_line).strip()
        if not content:
            continue
        fields = content.split()
        if fields[0] != "provider" or len(fields) < 3:
            raise CycloError(
                f"{selected}:{line_number}: expected: provider INSTANCE SOURCE "
                "[context=PATH] REQUIREMENT=TARGET ... [-- ARGUMENT ...]"
            )
        instance = fields[1]
        if (
            not _INSTANCE_RE.fullmatch(instance)
            or instance == "gateway"
            or instance in available
        ):
            raise CycloError(f"{selected}:{line_number}: invalid or duplicate provider instance {instance!r}")
        configured_source = fields[2]
        if configured_source == "~" or configured_source.startswith("~/"):
            raise CycloError(f"{selected}:{line_number}: component paths do not expand '~'")
        lexical_source = Path(configured_source)
        if not lexical_source.is_absolute():
            lexical_source = selected.parent / lexical_source
        source = _canonical_directory(lexical_source, "component source")
        _regular_file(source / "Dockerfile", "component Dockerfile")
        declaration = parse_declaration(source / "component.conf")
        if PROVIDER_INTERFACE not in declaration.provides:
            raise CycloError(
                f"{selected}:{line_number}: provider component must provide {PROVIDER_INTERFACE}"
            )
        if not any(item.service == PROVIDER_INTERFACE for item in declaration.requires):
            raise CycloError(
                f"{selected}:{line_number}: provider component must require an upstream {PROVIDER_INTERFACE}"
            )

        separators = [index for index, field in enumerate(fields) if field == "--"]
        if len(separators) > 1:
            raise CycloError(f"{selected}:{line_number}: provider line contains more than one '--'")
        boundary = separators[0] if separators else len(fields)
        arguments = tuple(fields[boundary + 1 :]) if separators else ()
        requirements = {item.name: item for item in declaration.requires}
        bindings: dict[str, str] = {}
        configured_context: str | None = None
        for setting in fields[3:boundary]:
            key, separator, value = setting.partition("=")
            if not separator or not key:
                raise CycloError(
                    f"{selected}:{line_number}: expected REQUIREMENT=TARGET before '--', got {setting!r}"
                )
            if key == "context":
                if "context" in requirements:
                    raise CycloError(f"{selected}:{line_number}: requirement name 'context' is reserved")
                if configured_context is not None or not value:
                    raise CycloError(f"{selected}:{line_number}: invalid duplicate or empty context setting")
                configured_context = value
                continue
            requirement = requirements.get(key)
            if requirement is None or key in bindings or not value:
                raise CycloError(f"{selected}:{line_number}: invalid or duplicate binding {setting!r}")
            target_services = available.get(value)
            if target_services is None:
                raise CycloError(
                    f"{selected}:{line_number}: binding {key} targets unknown or later provider {value}"
                )
            if requirement.service not in target_services:
                raise CycloError(
                    f"{selected}:{line_number}: binding {key} requires {requirement.service}, "
                    f"but {value} does not provide it"
                )
            bindings[key] = value
        for requirement in declaration.requires:
            if requirement.name not in bindings:
                suggested = "gateway" if requirement.name == "upstream" else "TARGET"
                raise CycloError(
                    f"{selected}:{line_number}: missing binding {requirement.name}={suggested}"
                )

        if configured_context is None:
            build_context = source
        else:
            if configured_context == "~" or configured_context.startswith("~/"):
                raise CycloError(f"{selected}:{line_number}: build context paths do not expand '~'")
            lexical_context = Path(configured_context)
            if not lexical_context.is_absolute():
                lexical_context = source / lexical_context
            build_context = _canonical_directory(lexical_context, "build context")
            if not source.is_relative_to(build_context):
                raise CycloError(
                    f"{selected}:{line_number}: component source must be inside its build context"
                )

        providers.append(
            ProviderDefinition(
                instance,
                source,
                build_context,
                declaration,
                tuple(bindings.items()),
                arguments,
                line_number,
            )
        )
        available[instance] = set(declaration.provides)
    return Assembly(selected, tuple(providers), generation)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


def connect_unary(
    socket_path: Path,
    service: str,
    method: str,
    request: Mapping[str, object] | None = None,
    *,
    timeout: float = 2.0,
) -> dict[str, object]:
    if not socket_path.is_absolute():
        raise CycloError("component socket path must be absolute")
    if not _SERVICE_RE.fullmatch(service):
        raise CycloError(f"invalid component service name: {service!r}")
    if not _METHOD_RE.fullmatch(method):
        raise CycloError(f"invalid component method name: {method!r}")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not isfinite(timeout)
        or timeout <= 0
    ):
        raise CycloError("component RPC timeout must be a positive finite number")
    if request is not None and not isinstance(request, Mapping):
        raise CycloError("component RPC request must be a JSON object")
    try:
        body = json.dumps(
            {} if request is None else request,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CycloError("component RPC request is not valid JSON") from exc
    if len(body) > MAX_RPC_BYTES:
        raise CycloError(f"component RPC request exceeds {MAX_RPC_BYTES} bytes")
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request(
            "POST",
            f"/{service}/{method}",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        payload = response.read(MAX_RPC_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise CycloError(f"component RPC failed at {socket_path}: {exc}") from exc
    finally:
        connection.close()
    if len(payload) > MAX_RPC_BYTES:
        raise CycloError(f"component RPC response exceeds {MAX_RPC_BYTES} bytes")
    try:
        document = (
            json.loads(
                payload.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
            if payload
            else {}
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise CycloError("component RPC returned invalid JSON") from exc
    if response.status != 200:
        detail = document.get("message") if isinstance(document, dict) else None
        raise CycloError(
            f"component RPC {service}/{method} failed ({response.status}): "
            f"{detail if isinstance(detail, str) and detail else response.reason}"
        )
    if not isinstance(document, dict):
        raise CycloError("component RPC response is not a JSON object")
    return document


def component_ready(socket_path: Path, *, timeout: float = 2.0) -> bool:
    try:
        response = connect_unary(
            socket_path,
            COMPONENT_INTERFACE,
            "Health",
            timeout=timeout,
        )
    except CycloError:
        return False
    return response.get("status") == "HEALTH_STATUS_READY"


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    read_only: bool = False
    type: str = "bind"


@dataclass(frozen=True)
class Deployment:
    instance: str
    component_type: str
    source: Path
    build_context: Path
    image: str
    container: str
    system: str
    arguments: tuple[str, ...]
    mounts: tuple[Mount, ...]
    network: str
    lifecycle: str = "provider"
    preserve_volumes: bool = False


@dataclass(frozen=True)
class DockerStatus:
    image_id: str | None
    container_id: str | None
    running: bool
    lifecycle: str
    engine_health: str
    current: bool


@dataclass(frozen=True)
class ComponentStatus:
    instance: str
    component_type: str
    dependencies_ready: bool
    health_ready: bool
    ready: bool
    docker: DockerStatus


@dataclass(frozen=True)
class GatewayStatus:
    socket_path: Path
    store_ready: bool
    health_ready: bool
    ready: bool
    docker: DockerStatus


@dataclass(frozen=True)
class StackStatus:
    generation: str
    provider_socket_path: Path
    gateway: GatewayStatus
    components: tuple[ComponentStatus, ...]
    ready: bool


class ComponentDocker:
    """One exact Docker lifecycle shared by every Cyclo component type."""

    def call(
        self,
        arguments: Sequence[str],
        *,
        capture: bool = True,
        check: bool = True,
        input_data: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.run(
                ["docker", *arguments],
                text=True,
                input=input_data,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CycloError("Docker is not installed or not on PATH") from exc
        if check and process.returncode != 0:
            detail = ((process.stderr or "") + (process.stdout or "")).strip()
            raise CycloError(
                f"Docker command failed ({process.returncode}): "
                f"{detail or 'docker ' + ' '.join(arguments)}"
            )
        return process

    def available(self) -> tuple[bool, str]:
        try:
            result = self.call(
                ["info", "--format", "{{.ServerVersion}}"], capture=True, check=False
            )
        except CycloError as exc:
            return False, str(exc)
        detail = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, detail

    def inspect(
        self, kind: str, reference: str, *, missing: bool = True
    ) -> dict[str, object] | None:
        result = self.call(
            [kind, "inspect", "--", reference], capture=True, check=False
        )
        if result.returncode != 0:
            detail = ((result.stderr or "") + (result.stdout or "")).strip()
            lowered = detail.lower()
            markers = {
                "container": ("no such container", "no such object"),
                "image": ("no such image", "no such object"),
                "volume": ("no such volume",),
            }.get(kind, ())
            if (
                missing
                and reference.lower() in lowered
                and any(marker in lowered for marker in markers)
            ):
                return None
            raise CycloError(
                f"cannot inspect Docker {kind} {reference}: {detail or 'unknown Docker error'}"
            )
        try:
            document = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise CycloError(f"cannot parse Docker {kind} inspection for {reference}") from exc
        if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
            raise CycloError(f"invalid Docker {kind} inspection for {reference}")
        return document[0]

    @staticmethod
    def _labels(info: Mapping[str, object]) -> dict[str, str]:
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if labels is None:
            return {}
        if not isinstance(labels, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in labels.items()
        ):
            raise CycloError("cannot parse Docker resource labels")
        return dict(labels)

    @staticmethod
    def _image_id(info: Mapping[str, object]) -> str:
        value = info.get("Id")
        if not isinstance(value, str) or not _IMAGE_ID_RE.fullmatch(value):
            raise CycloError("cannot parse Docker image ID")
        return value

    @staticmethod
    def _container_id(info: Mapping[str, object]) -> str:
        value = info.get("Id")
        if not isinstance(value, str) or not _CONTAINER_ID_RE.fullmatch(value):
            raise CycloError("cannot parse Docker container ID")
        return value

    @staticmethod
    def _expected_labels(deployment: Deployment) -> dict[str, str]:
        return {
            LABEL_OWNED: "1",
            LABEL_SYSTEM: deployment.system,
            LABEL_INSTANCE: deployment.instance,
            LABEL_LIFECYCLE: deployment.lifecycle,
            LABEL_TYPE: deployment.component_type,
        }

    def _require_owned(
        self, deployment: Deployment, info: Mapping[str, object], *, image: bool
    ) -> None:
        labels = self._labels(info)
        expected = self._expected_labels(deployment)
        ownership = {
            key: expected[key]
            for key in (LABEL_OWNED, LABEL_SYSTEM, LABEL_INSTANCE, LABEL_LIFECYCLE)
        }
        if any(labels.get(key) != value for key, value in ownership.items()):
            kind = "image" if image else "container"
            raise CycloError(f"refusing Docker {kind} not owned by this Cyclo component")
        if image:
            self._image_id(info)
        else:
            self._container_id(info)
            raw_name = info.get("Name")
            name = raw_name[1:] if isinstance(raw_name, str) and raw_name.startswith("/") else raw_name
            if name != deployment.container:
                raise CycloError(f"refusing mislabeled Docker container: {deployment.container}")

    def _validate_image(self, deployment: Deployment, info: Mapping[str, object]) -> None:
        self._require_owned(deployment, info, image=True)
        labels = self._labels(info)
        if any(
            labels.get(key) != value
            for key, value in self._expected_labels(deployment).items()
        ):
            raise CycloError(f"Docker image has incomplete component labels: {deployment.image}")
        config = info.get("Config")
        if not isinstance(config, Mapping):
            raise CycloError("cannot parse Docker image configuration")
        entrypoint = config.get("Entrypoint")
        user = config.get("User")
        health = config.get("Healthcheck")
        if (
            not isinstance(entrypoint, list)
            or not entrypoint
            or any(not isinstance(item, str) or not item for item in entrypoint)
        ):
            raise CycloError("component image must define OCI ENTRYPOINT")
        user_match = re.fullmatch(r"(\d+):(\d+)", user) if isinstance(user, str) else None
        if user_match is None or int(user_match.group(1)) <= 0 or int(user_match.group(2)) <= 0:
            raise CycloError("component image must define a positive numeric USER UID:GID")
        test = health.get("Test") if isinstance(health, Mapping) else None
        if (
            not isinstance(test, list)
            or len(test) < 2
            or test[0] not in {"CMD", "CMD-SHELL"}
            or any(not isinstance(item, str) for item in test)
        ):
            raise CycloError("component image must define HEALTHCHECK")
        for field, message in (
            ("ExposedPorts", "Unix-socket component image must not expose TCP ports"),
            ("Volumes", "component image must not declare OCI volumes"),
        ):
            value = config.get(field)
            if isinstance(value, Mapping) and value:
                raise CycloError(message)

    def build_image(
        self,
        image: str,
        arguments: Sequence[str],
        validate: Callable[[Mapping[str, object]], None],
        *,
        before_promote: Callable[[], None] | None = None,
    ) -> str:
        """Build, validate, and transactionally promote one Docker image."""

        if not arguments or arguments[0] != "build":
            raise CycloError("Cyclo image build arguments must start with 'build'")
        repository = image.rsplit(":", 1)[0]
        candidate = f"{repository}:candidate-{os.getpid()}-{uuid.uuid4()}"
        directory = Path(tempfile.mkdtemp(prefix="cyclo-image-build-"))
        iidfile = directory / "image-id"
        try:
            self.call(
                [
                    "build",
                    "--tag",
                    candidate,
                    "--iidfile",
                    str(iidfile),
                    *arguments[1:],
                ],
                capture=False,
            )
            try:
                image_id = iidfile.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise CycloError("Docker build did not publish an image ID") from exc
            if not _IMAGE_ID_RE.fullmatch(image_id):
                raise CycloError("Docker build returned an invalid image ID")
            built = self.inspect("image", image_id, missing=False)
            candidate_info = self.inspect("image", candidate, missing=False)
            assert built is not None and candidate_info is not None
            if self._image_id(candidate_info) != image_id:
                raise CycloError("Docker candidate tag does not reference the completed build")
            validate(built)
            if before_promote is not None:
                before_promote()
            self.call(["image", "tag", "--", image_id, image])
            official = self.inspect("image", image, missing=False)
            assert official is not None
            if self._image_id(official) != image_id:
                raise CycloError("Docker official tag changed during build promotion")
            return image_id
        finally:
            self.call(["image", "rm", "--", candidate], check=False)
            shutil.rmtree(directory, ignore_errors=True)

    def build(self, deployment: Deployment) -> str:
        current = self.inspect("image", deployment.image)
        if current is not None:
            self._require_owned(deployment, current, image=True)
        labels = [
            item
            for key, value in self._expected_labels(deployment).items()
            for item in ("--label", f"{key}={value}")
        ]
        return self.build_image(
            deployment.image,
            [
                "build",
                *labels,
                "--file",
                str(deployment.source / "Dockerfile"),
                str(deployment.build_context),
            ],
            lambda info: self._validate_image(deployment, info),
        )

    def require_image(self, deployment: Deployment) -> str:
        image = self.inspect("image", deployment.image)
        if image is None:
            raise CycloError(f"component image is not built: {deployment.instance}")
        self._validate_image(deployment, image)
        return self._image_id(image)

    @staticmethod
    def _lifecycle(container: Mapping[str, object]) -> str:
        state = container.get("State")
        if not isinstance(state, Mapping):
            raise CycloError("cannot parse Docker container state")
        status = str(state.get("Status") or "").lower()
        if state.get("Dead") is True or status == "dead":
            return "dead"
        if state.get("Restarting") is True or status == "restarting":
            return "restarting"
        if state.get("Paused") is True or status == "paused":
            return "paused"
        if state.get("Running") is True or status == "running":
            return "running"
        return "stopped"

    @staticmethod
    def _engine_health(container: Mapping[str, object]) -> str:
        state = container.get("State")
        health = state.get("Health") if isinstance(state, Mapping) else None
        status = health.get("Status") if isinstance(health, Mapping) else None
        return status if status in {"starting", "healthy", "unhealthy"} else "missing"

    @staticmethod
    def _mount_argument(mount: Mount) -> str:
        if mount.type not in {"bind", "volume"}:
            raise CycloError(f"unsupported Docker mount type: {mount.type}")
        if "," in mount.source or "," in mount.destination:
            raise CycloError(f"Docker mount paths cannot contain a comma: {mount.source}")
        result = f"type={mount.type},src={mount.source},dst={mount.destination}"
        return result + (",readonly" if mount.read_only else "")

    def _configuration_current(
        self,
        deployment: Deployment,
        image: Mapping[str, object],
        container: Mapping[str, object],
    ) -> bool:
        try:
            host = container["HostConfig"]
            config = container["Config"]
            image_config = image["Config"]
            if not all(isinstance(value, Mapping) for value in (host, config, image_config)):
                return False
            restart = host.get("RestartPolicy")
            security = host.get("SecurityOpt")
            dropped = host.get("CapDrop")
            added = host.get("CapAdd")
            devices = host.get("Devices")
            device_requests = host.get("DeviceRequests")
            tmpfs = host.get("Tmpfs")
            ulimits = host.get("Ulimits")
            nofile = next(
                (
                    value
                    for value in ulimits
                    if isinstance(value, Mapping) and value.get("Name") == "nofile"
                ),
                None,
            ) if isinstance(ulimits, list) else None
            network_settings = container.get("NetworkSettings")
            networks = (
                network_settings.get("Networks")
                if isinstance(network_settings, Mapping)
                else None
            )
            published_ports = (
                network_settings.get("Ports")
                if isinstance(network_settings, Mapping)
                else None
            )
            tmpfs_value = tmpfs.get("/tmp") if isinstance(tmpfs, Mapping) else None
            tmpfs_flags = (
                {flag for flag in tmpfs_value.split(",") if flag}
                if isinstance(tmpfs_value, str)
                else set()
            )
            allowed_tmpfs_flags = {
                "rw",
                "noexec",
                "nosuid",
                "nodev",
                "size=64m",
                "size=67108864",
            }
            if (
                host.get("NetworkMode") != deployment.network
                or host.get("ReadonlyRootfs") is not True
                or host.get("Privileged") is True
                or host.get("PidMode") != ""
                or host.get("IpcMode") != "private"
                or host.get("UTSMode") != ""
                or host.get("UsernsMode") != ""
                or host.get("CgroupnsMode") != "private"
                or host.get("PidsLimit") != 256
                or not isinstance(restart, Mapping)
                or restart.get("Name") != "unless-stopped"
                or security != ["no-new-privileges"]
                or not isinstance(dropped, list)
                or "ALL" not in {str(item).upper() for item in dropped}
                or added not in (None, [])
                or devices not in (None, [])
                or device_requests not in (None, [])
                or not isinstance(nofile, Mapping)
                or nofile.get("Soft") != 1024
                or nofile.get("Hard") != 1024
                or not isinstance(tmpfs, Mapping)
                or set(tmpfs) != {"/tmp"}
                or not {"rw", "noexec", "nosuid", "nodev"}.issubset(tmpfs_flags)
                or not ({"size=64m", "size=67108864"} & tmpfs_flags)
                or not tmpfs_flags.issubset(allowed_tmpfs_flags)
                or not isinstance(networks, Mapping)
                or set(networks) != {deployment.network}
                or host.get("PortBindings") not in (None, {})
                or published_ports not in (None, {})
            ):
                return False
            if (
                config.get("User") != image_config.get("User")
                or config.get("Entrypoint") != image_config.get("Entrypoint")
                or config.get("Healthcheck") != image_config.get("Healthcheck")
                or config.get("Env") != image_config.get("Env")
                or config.get("WorkingDir") != image_config.get("WorkingDir")
                or config.get("Cmd") != ["serve", *deployment.arguments]
            ):
                return False
            if any(
                self._labels(container).get(key) != value
                for key, value in self._expected_labels(deployment).items()
            ):
                return False
            actual_mounts = container.get("Mounts")
            if not isinstance(actual_mounts, list) or len(actual_mounts) != len(deployment.mounts):
                return False
            observed = {
                mount.get("Destination"): mount
                for mount in actual_mounts
                if isinstance(mount, Mapping)
            }
            if len(observed) != len(actual_mounts):
                return False
            for expected in deployment.mounts:
                actual = observed.get(expected.destination)
                if (
                    not isinstance(actual, Mapping)
                    or actual.get("Type") != expected.type
                    or (
                        expected.type == "bind"
                        and actual.get("Source") != expected.source
                    )
                    or (
                        expected.type == "volume"
                        and actual.get("Name") != expected.source
                    )
                    or actual.get("RW") is not (not expected.read_only)
                ):
                    return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def status(self, deployment: Deployment) -> DockerStatus:
        image = self.inspect("image", deployment.image)
        container = self.inspect("container", deployment.container)
        if image is not None:
            self._require_owned(deployment, image, image=True)
        if container is not None:
            self._require_owned(deployment, container, image=False)
        image_id = self._image_id(image) if image is not None else None
        if container is None:
            return DockerStatus(image_id, None, False, "absent", "missing", False)
        container_id = self._container_id(container)
        container_image = container.get("Image")
        if not isinstance(container_image, str) or not _IMAGE_ID_RE.fullmatch(container_image):
            raise CycloError("cannot parse Docker container image ID")
        lifecycle = self._lifecycle(container)
        valid_image = False
        if image is not None:
            try:
                self._validate_image(deployment, image)
                valid_image = True
            except CycloError:
                pass
        current = bool(
            image is not None
            and valid_image
            and image_id == container_image
            and self._configuration_current(deployment, image, container)
        )
        return DockerStatus(
            image_id,
            container_id,
            lifecycle == "running",
            lifecycle,
            self._engine_health(container),
            current,
        )

    def start(self, deployment: Deployment) -> str:
        existing = self.inspect("container", deployment.container)
        if existing is not None:
            self._require_owned(deployment, existing, image=False)
            raise CycloError(
                f"component container already exists; restart it: {deployment.instance}"
            )
        image_id = self.require_image(deployment)
        labels = [
            item
            for key, value in self._expected_labels(deployment).items()
            for item in ("--label", f"{key}={value}")
        ]
        mounts = [
            item
            for mount in deployment.mounts
            for item in ("--mount", self._mount_argument(mount))
        ]
        result = self.call(
            [
                "run",
                "--detach",
                "--name",
                deployment.container,
                *labels,
                "--restart",
                "unless-stopped",
                "--stop-timeout",
                "10",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--ipc",
                "private",
                "--cgroupns",
                "private",
                "--pids-limit",
                "256",
                "--ulimit",
                "nofile=1024:1024",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=64m",
                "--network",
                deployment.network,
                *mounts,
                image_id,
                "serve",
                *deployment.arguments,
            ]
        )
        container_id = (result.stdout or "").strip()
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            raise CycloError("Docker run returned an invalid container ID")
        try:
            status = self.status(deployment)
            if status.container_id != container_id or not status.running or not status.current:
                raise CycloError(
                    f"component did not start with the requested isolation: {deployment.instance}"
                )
        except BaseException:
            self._remove_started(deployment, container_id)
            raise
        return container_id

    def _remove_started(self, deployment: Deployment, identifier: str) -> None:
        """Best-effort rollback of only the container created by ``start``."""

        try:
            container = self.inspect("container", identifier)
            if container is None:
                return
            self._require_owned(deployment, container, image=False)
            command = ["rm", "--force"]
            if not deployment.preserve_volumes:
                command.append("--volumes")
            command.append(identifier)
            self.call(command, check=False)
        except Exception:
            # Preserve the causal startup failure. An explicit stop can retry.
            pass

    def stop(self, deployment: Deployment, expected_id: str | None = None) -> bool:
        if expected_id is not None and not _CONTAINER_ID_RE.fullmatch(expected_id):
            raise CycloError("invalid expected container ID")
        container = self.inspect("container", expected_id or deployment.container)
        if container is None:
            return False
        self._require_owned(deployment, container, image=False)
        container_id = self._container_id(container)
        if expected_id is not None and expected_id != container_id:
            raise CycloError("Docker returned a different container than requested")
        if self._lifecycle(container) != "stopped":
            self.call(["stop", "--timeout", "10", container_id])
        command = ["rm", container_id]
        if not deployment.preserve_volumes:
            command.insert(1, "--volumes")
        self.call(command)
        return True

    def stop_providers(
        self,
        system: str,
        *,
        excluding: Iterable[str] = (),
    ) -> tuple[str, ...]:
        skipped = set(excluding)
        result = self.call(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={LABEL_OWNED}=1",
                "--filter",
                f"label={LABEL_SYSTEM}={system}",
                "--filter",
                f"label={LABEL_LIFECYCLE}=provider",
            ]
        )
        stopped: list[str] = []
        for identifier in dict.fromkeys((result.stdout or "").splitlines()):
            if not identifier:
                continue
            if not _CONTAINER_ID_RE.fullmatch(identifier):
                raise CycloError("Docker returned an invalid component container ID")
            info = self.inspect("container", identifier)
            if info is None:
                continue
            labels = self._labels(info)
            instance = labels.get(LABEL_INSTANCE)
            raw_name = info.get("Name")
            name = raw_name[1:] if isinstance(raw_name, str) and raw_name.startswith("/") else raw_name
            if (
                labels.get(LABEL_OWNED) != "1"
                or labels.get(LABEL_SYSTEM) != system
                or labels.get(LABEL_LIFECYCLE) != "provider"
                or not isinstance(instance, str)
                or not _INSTANCE_RE.fullmatch(instance)
                or name != provider_name(system, instance)
            ):
                raise CycloError(f"invalid Cyclo ownership labels on container {identifier}")
            if instance in skipped:
                continue
            if self._lifecycle(info) != "stopped":
                self.call(["stop", "--timeout", "10", identifier])
            self.call(["rm", "--volumes", identifier])
            stopped.append(instance)
        return tuple(stopped)

    def logs(self, deployment: Deployment, lines: int = 80) -> str:
        info = self.inspect("container", deployment.container)
        if info is None:
            return ""
        self._require_owned(deployment, info, image=False)
        result = self.call(
            ["logs", "--tail", str(lines), self._container_id(info)], check=False
        )
        return ((result.stdout or "") + (result.stderr or "")).strip()


def _deployment_names(
    state_root: Path, instance: str, *, gateway: bool = False
) -> tuple[str, str, str]:
    system = installation_id(state_root)
    stem = gateway_name(system) if gateway else provider_name(system, instance)
    return system, f"{stem}:latest", stem


class Gateway:
    def __init__(
        self,
        state_root: Path,
        *,
        docker: ComponentDocker | None = None,
        network: str = "bridge",
    ) -> None:
        self.state_root = state_root.expanduser().resolve()
        if not _NETWORK_RE.fullmatch(network) or network in {"default", "host", "none"}:
            raise CycloError("gateway network must be 'bridge' or a named Docker network")
        self.network = network
        self.root = self.state_root / "gateway"
        self.config_dir = self.root / "config"
        self.socket_dir = self.root / "socket"
        self.socket_path = self.socket_dir / COMPONENT_SOCKET
        source = component_sources_root() / "gateway"
        system, image, container = _deployment_names(
            self.state_root, "gateway", gateway=True
        )
        self.store_volume = f"{container}-state"
        self.deployment = Deployment(
            "gateway",
            "gateway",
            source,
            component_sources_root(),
            image,
            container,
            system,
            (),
            (
                Mount(self.store_volume, "/var/lib/cyclo-gateway", type="volume"),
                Mount(str(self.config_dir), "/etc/cyclo-gateway", read_only=True),
                Mount(str(self.socket_dir), str(CONTAINER_SOCKET_ROOT)),
            ),
            network,
            lifecycle="gateway",
            preserve_volumes=True,
        )
        self.docker = docker or ComponentDocker()

    def _prepare_root(self) -> None:
        _ensure_directory(self.state_root, 0o700)
        _ensure_directory(self.root, 0o700)

    def _prepare(self) -> None:
        self._prepare_root()
        _ensure_directory(self.config_dir, 0o755)
        _ensure_directory(self.socket_dir, 0o777)
        entries = list(self.socket_dir.iterdir())
        if entries and not (
            len(entries) == 1
            and entries[0].name == COMPONENT_SOCKET
            and stat.S_ISSOCK(entries[0].lstat().st_mode)
            and not entries[0].is_symlink()
        ):
            raise CycloError(
                f"gateway socket directory must be empty or contain only {COMPONENT_SOCKET}: {self.socket_dir}"
            )

    def _volume_labels(self) -> dict[str, str]:
        return {
            LABEL_OWNED: "1",
            LABEL_SYSTEM: self.deployment.system,
            LABEL_INSTANCE: "gateway",
            LABEL_LIFECYCLE: "gateway",
            LABEL_TYPE: "gateway-state",
        }

    def _volume_ready(self, *, create: bool = False) -> bool:
        volume = self.docker.inspect("volume", self.store_volume)
        if volume is None and create:
            labels = [
                item
                for key, value in self._volume_labels().items()
                for item in ("--label", f"{key}={value}")
            ]
            self.docker.call(["volume", "create", *labels, "--name", self.store_volume])
            volume = self.docker.inspect("volume", self.store_volume, missing=False)
        if volume is None:
            return False
        labels = volume.get("Labels")
        options = volume.get("Options")
        if (
            volume.get("Name") != self.store_volume
            or volume.get("Driver") != "local"
            or volume.get("Scope") != "local"
            or not isinstance(labels, Mapping)
            or dict(labels) != self._volume_labels()
            or (options not in (None, {}) and not (isinstance(options, Mapping) and not options))
        ):
            raise CycloError(f"refusing foreign gateway credential volume: {self.store_volume}")
        return True

    def _require_exclusive_store(self, allowed: str | None = None) -> None:
        result = self.docker.call(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"volume={self.store_volume}",
            ]
        )
        for identifier in set((result.stdout or "").splitlines()):
            if identifier and identifier != allowed:
                if not _CONTAINER_ID_RE.fullmatch(identifier):
                    raise CycloError("Docker returned an invalid credential-volume user")
                raise CycloError(f"credential volume is mounted by another container: {identifier}")

    def build(self) -> str:
        declaration = parse_declaration(self.deployment.source / "component.conf")
        if (
            declaration.name != "gateway"
            or declaration.requires
            or set(declaration.provides) != {COMPONENT_INTERFACE, PROVIDER_INTERFACE}
        ):
            raise CycloError(
                "gateway component must provide exactly Component and Provider with no requirements"
            )
        return self.docker.build(self.deployment)

    def status(self) -> GatewayStatus:
        docker_status = self.docker.status(self.deployment)
        store_ready = self._volume_ready()
        health_ready = bool(
            docker_status.current
            and docker_status.running
            and component_ready(self.socket_path)
        )
        if health_ready:
            try:
                entries = list(self.socket_dir.iterdir())
                health_ready = bool(
                    len(entries) == 1
                    and entries[0].name == COMPONENT_SOCKET
                    and stat.S_ISSOCK(entries[0].lstat().st_mode)
                    and not entries[0].is_symlink()
                )
            except OSError:
                health_ready = False
        return GatewayStatus(
            self.socket_path,
            store_ready,
            health_ready,
            store_ready and health_ready,
            docker_status,
        )

    def _wait_ready(self, timeout: float = 20.0) -> GatewayStatus:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status.ready:
                return status
            if not status.docker.running or not status.docker.current:
                logs = self.docker.logs(self.deployment)
                raise CycloError(
                    "gateway stopped or changed during startup"
                    + (f"\n{logs}" if logs else "")
                )
            time.sleep(0.1)
        logs = self.docker.logs(self.deployment)
        raise CycloError("timed out waiting for gateway" + (f"\n{logs}" if logs else ""))

    def start(self) -> GatewayStatus:
        self._prepare()
        self.docker.require_image(self.deployment)
        self._volume_ready(create=True)
        existing = self.status()
        if existing.docker.container_id:
            if existing.ready:
                return existing
            raise CycloError("gateway container already exists but is not ready; restart it")
        self._require_exclusive_store()
        identifier = self.docker.start(self.deployment)
        try:
            return self._wait_ready()
        except BaseException:
            try:
                self.docker.stop(self.deployment, identifier)
            except Exception:
                pass
            raise

    def stop(self) -> bool:
        return self.docker.stop(self.deployment)

    def restart(self, *, build: bool = False) -> GatewayStatus:
        self._prepare()
        if build:
            self.build()
        self.docker.require_image(self.deployment)
        self._volume_ready(create=True)
        current = self.docker.status(self.deployment)
        self._require_exclusive_store(current.container_id)
        self.stop()
        return self.start()

    def _tool(
        self,
        command: Sequence[str],
        *,
        volume: bool,
        network: str = "none",
        interactive: bool = False,
        input_data: str | None = None,
        capture: bool = True,
        volume_read_only: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        image_id = self.docker.require_image(self.deployment)
        arguments = [
            "run",
            "--rm",
            *(["--interactive"] if interactive else []),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--ipc",
            "private",
            "--cgroupns",
            "private",
            "--pids-limit",
            "256",
            "--ulimit",
            "nofile=1024:1024",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network",
            network,
        ]
        if volume:
            self._volume_ready(create=True)
            arguments.extend(
                [
                    "--mount",
                    (
                        f"type=volume,src={self.store_volume},dst=/var/lib/cyclo-gateway"
                        + (",readonly" if volume_read_only else "")
                    ),
                ]
            )
        arguments.extend([image_id, *command])
        return self.docker.call(
            arguments,
            capture=capture,
            input_data=input_data,
        )

    def providers(self) -> str:
        return (self._tool(["providers"], volume=False).stdout or "").rstrip()

    def usage(self) -> dict[str, object]:
        result = self._tool(["usage"], volume=True, volume_read_only=True)
        try:
            document = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise CycloError("gateway usage command returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise CycloError("gateway usage command did not return an object")
        return document

    def login(self, arguments: Sequence[str]) -> None:
        if not arguments or any(not isinstance(item, str) or not item for item in arguments):
            raise CycloError("gateway login requires a provider")
        normalized = list(arguments)
        indexes = [index for index, value in enumerate(normalized) if value == "--api-key-env"]
        if len(indexes) > 1:
            raise CycloError("--api-key-env may be used only once")
        input_data: str | None = None
        if indexes:
            index = indexes[0]
            name = normalized[index + 1] if index + 1 < len(normalized) else ""
            if not _ENVIRONMENT_RE.fullmatch(name):
                raise CycloError("--api-key-env requires an environment variable name")
            value = os.environ.get(name)
            if not value:
                raise CycloError(f"environment variable {name} is empty or unset")
            normalized[index : index + 2] = ["--api-key-stdin"]
            input_data = value + "\n"
        api_key = "--api-key-stdin" in normalized
        current = self.docker.status(self.deployment)
        if current.running and not current.current:
            raise CycloError("running gateway is stale; restart it before login")
        self._volume_ready(create=True)
        self._require_exclusive_store(current.container_id)
        self._tool(
            ["login", *normalized],
            volume=True,
            network="none" if api_key else self.network,
            interactive=True,
            input_data=input_data,
            capture=False,
        )

    def destroy_store(self) -> bool:
        volume = self.docker.inspect("volume", self.store_volume)
        if volume is None:
            return False
        self._volume_ready()
        current = self.docker.status(self.deployment)
        self._require_exclusive_store(current.container_id)
        self.stop()
        self._require_exclusive_store()
        self.docker.call(["volume", "rm", self.store_volume])
        return True


class ProviderStack:
    def __init__(
        self,
        state_root: Path,
        config_path: Path,
        *,
        gateway: Gateway | None = None,
        docker: ComponentDocker | None = None,
        load_config: bool = True,
    ) -> None:
        self.state_root = state_root.expanduser().resolve()
        self.config_path = config_path.expanduser()
        self.docker = docker or ComponentDocker()
        self.gateway = gateway or Gateway(self.state_root, docker=self.docker)
        self.assembly = (
            load_assembly(self.config_path)
            if load_config
            else Assembly(Path(os.path.abspath(self.config_path)), (), "")
        )
        self.deployments = self._deployments()
        self.provider_socket_path = (
            self.socket_dir(self.assembly.providers[-1].instance) / COMPONENT_SOCKET
            if self.assembly.providers
            else self.gateway.socket_path
        )

    @property
    def system(self) -> str:
        return installation_id(self.state_root)

    @property
    def sockets_root(self) -> Path:
        return self.state_root / "sockets"

    def socket_dir(self, instance: str) -> Path:
        return self.sockets_root / instance

    def _deployments(self) -> tuple[Deployment, ...]:
        socket_dirs = {"gateway": self.gateway.socket_dir}
        socket_dirs.update(
            {
                provider.instance: self.socket_dir(provider.instance)
                for provider in self.assembly.providers
            }
        )
        deployments: list[Deployment] = []
        for provider in self.assembly.providers:
            _system, image, container = _deployment_names(
                self.state_root, provider.instance
            )
            mounts = [
                Mount(
                    str(socket_dirs[provider.instance]),
                    str(CONTAINER_SOCKET_ROOT),
                )
            ]
            for requirement in provider.declaration.requires:
                mounts.append(
                    Mount(
                        str(socket_dirs[provider.target(requirement.name)]),
                        str(CONTAINER_REQUIREMENT_ROOT / requirement.name),
                        read_only=True,
                    )
                )
            deployments.append(
                Deployment(
                    provider.instance,
                    provider.declaration.name,
                    provider.source,
                    provider.build_context,
                    image,
                    container,
                    self.system,
                    provider.arguments,
                    tuple(mounts),
                    "none",
                )
            )
        return tuple(deployments)

    def _prepare(self) -> None:
        _ensure_directory(self.state_root, 0o700)
        _ensure_directory(self.sockets_root, 0o700)
        for provider in self.assembly.providers:
            output = _ensure_directory(self.socket_dir(provider.instance), 0o777)
            requirements = _ensure_directory(output / "requirements", 0o755)
            for requirement in provider.declaration.requires:
                _ensure_directory(requirements / requirement.name, 0o755)

    def check(self) -> int:
        return len(self.assembly.providers)

    def build(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (deployment.instance, self.docker.build(deployment))
            for deployment in self.deployments
        )

    def _gateway_status(self) -> GatewayStatus:
        return self.gateway.status()

    def status(self) -> StackStatus:
        gateway_status = self._gateway_status()
        ready_by_name = {"gateway": gateway_status.ready}
        components: list[ComponentStatus] = []
        by_instance = {
            provider.instance: provider for provider in self.assembly.providers
        }
        for deployment in self.deployments:
            provider = by_instance[deployment.instance]
            docker_status = self.docker.status(deployment)
            dependencies_ready = all(
                ready_by_name.get(target, False)
                for _requirement, target in provider.bindings
            )
            health_ready = bool(
                docker_status.current
                and docker_status.running
                and component_ready(self.socket_dir(provider.instance) / COMPONENT_SOCKET)
            )
            ready = dependencies_ready and health_ready
            ready_by_name[provider.instance] = ready
            components.append(
                ComponentStatus(
                    provider.instance,
                    provider.declaration.name,
                    dependencies_ready,
                    health_ready,
                    ready,
                    docker_status,
                )
            )
        return StackStatus(
            self.assembly.generation,
            self.provider_socket_path,
            gateway_status,
            tuple(components),
            gateway_status.ready and all(item.ready for item in components),
        )

    def require_ready(self) -> StackStatus:
        status = self.status()
        if not status.gateway.ready:
            raise CycloError(
                "credential gateway is not ready; run `cyclo gateway restart --build`"
            )
        failures = [item.instance for item in status.components if not item.ready]
        if failures:
            raise CycloError(
                "provider stack is not ready: "
                + ", ".join(failures)
                + "; run `cyclo providers restart --build`"
            )
        return status

    def _wait_ready(self, deployment: Deployment, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        socket_path = self.socket_dir(deployment.instance) / COMPONENT_SOCKET
        while time.monotonic() < deadline:
            status = self.docker.status(deployment)
            if not status.running or not status.current:
                logs = self.docker.logs(deployment)
                raise CycloError(
                    f"component {deployment.instance} stopped or changed during startup"
                    + (f"\n{logs}" if logs else "")
                )
            if component_ready(socket_path):
                return
            time.sleep(0.1)
        logs = self.docker.logs(deployment)
        raise CycloError(
            f"timed out waiting for component {deployment.instance}"
            + (f"\n{logs}" if logs else "")
        )

    def start(self) -> StackStatus:
        if not self.gateway.status().ready:
            raise CycloError("gateway Component.Health is not ready")
        self._prepare()
        started: list[tuple[Deployment, str]] = []
        try:
            for deployment in self.deployments:
                current = self.docker.status(deployment)
                if current.container_id:
                    socket_path = self.socket_dir(deployment.instance) / COMPONENT_SOCKET
                    if current.current and current.running and component_ready(socket_path):
                        continue
                    raise CycloError(
                        f"component {deployment.instance} already exists but is not ready; restart it"
                    )
                identifier = self.docker.start(deployment)
                started.append((deployment, identifier))
                self._wait_ready(deployment)
        except BaseException:
            for deployment, identifier in reversed(started):
                try:
                    self.docker.stop(deployment, identifier)
                except Exception:
                    pass
            raise
        return self.status()

    def stop(self) -> tuple[str, ...]:
        stopped: list[str] = []
        for deployment in reversed(self.deployments):
            if self.docker.stop(deployment):
                stopped.append(deployment.instance)
        for instance in self.docker.stop_providers(self.system):
            if instance not in stopped:
                stopped.append(instance)
        return tuple(stopped)

    def restart(self, *, build: bool = False) -> StackStatus:
        if not self.gateway.status().ready:
            raise CycloError("gateway Component.Health is not ready")
        self._prepare()
        if build:
            self.build()
        for deployment in self.deployments:
            self.docker.require_image(deployment)
        self.stop()
        return self.start()

    def models_document(self) -> dict[str, object]:
        self.require_ready()
        response = connect_unary(
            self.provider_socket_path,
            PROVIDER_INTERFACE,
            "ListModels",
            timeout=10.0,
        )
        models = response.get("models")
        if not isinstance(models, list) or any(not isinstance(model, dict) for model in models):
            raise CycloError("provider stack returned an invalid model catalogue")
        return response

    def model_ids(self) -> tuple[str, ...]:
        result: list[str] = []
        for model in self.models_document()["models"]:  # type: ignore[index]
            model_id = model.get("id") if isinstance(model, dict) else None
            if not isinstance(model_id, str) or not model_id or model_id in result:
                raise CycloError("provider stack returned an invalid or duplicate model ID")
            result.append(model_id)
        return tuple(result)
