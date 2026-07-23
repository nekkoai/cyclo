from __future__ import annotations

import http.client
import json
import re
import socket
import stat
from dataclasses import dataclass
from importlib import resources
from math import isfinite
from pathlib import Path
from typing import Mapping

from .errors import CycloError


COMPONENT_INTERFACE = "cyclo.component.v1.Component"
PROVIDER_INTERFACE = "cyclo.provider.v1.Provider"
COMPONENT_SOCKET = "component.sock"
CONTAINER_SOCKET_ROOT = Path("/run/cyclo")
CONTAINER_REQUIREMENT_ROOT = CONTAINER_SOCKET_ROOT / "requirements"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RPC_BYTES = 16 * 1024 * 1024

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SERVICE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def component_sources_root() -> Path:
    """Return the component sources bundled with Cyclo."""

    return Path(str(resources.files("cyclo"))).resolve() / "components"


def is_component_name(value: object) -> bool:
    return isinstance(value, str) and bool(_NAME_RE.fullmatch(value))


def regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CycloError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise CycloError(f"{label} is not a regular file: {path}")
    return path


def canonical_directory(path: Path, label: str) -> Path:
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
    """Parse one language-neutral component interface declaration."""

    regular_file(path, "component declaration")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CycloError(f"cannot read component declaration {path}: {exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise CycloError(
            f"component declaration exceeds {MAX_CONFIG_BYTES} bytes: {path}"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CycloError(
            f"component declaration is not valid UTF-8: {path}"
        ) from exc

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
                raise CycloError(
                    f"{path}:{line_number}: expected: component NAME"
                )
            if name is not None:
                raise CycloError(
                    f"{path}:{line_number}: duplicate component declaration"
                )
            if not is_component_name(fields[1]):
                raise CycloError(
                    f"{path}:{line_number}: invalid component name {fields[1]!r}"
                )
            name = fields[1]
        elif directive == "provide":
            if len(fields) != 2 or not _SERVICE_RE.fullmatch(fields[1]):
                raise CycloError(
                    f"{path}:{line_number}: expected: provide SERVICE"
                )
            if fields[1] in provides:
                raise CycloError(
                    f"{path}:{line_number}: duplicate provided interface {fields[1]}"
                )
            provides.append(fields[1])
        elif directive == "require":
            if (
                len(fields) != 3
                or not is_component_name(fields[1])
                or not _SERVICE_RE.fullmatch(fields[2])
            ):
                raise CycloError(
                    f"{path}:{line_number}: expected: require NAME SERVICE"
                )
            if fields[1] in required_names:
                raise CycloError(
                    f"{path}:{line_number}: duplicate requirement {fields[1]}"
                )
            required_names.add(fields[1])
            requires.append(Requirement(fields[1], fields[2]))
        else:
            raise CycloError(
                f"{path}:{line_number}: unknown directive {directive}"
            )
    if name is None:
        raise CycloError(f"{path}:1: missing component declaration")
    if COMPONENT_INTERFACE not in provides:
        raise CycloError(
            f"{path}:1: every component must provide {COMPONENT_INTERFACE}"
        )
    return Declaration(name, tuple(provides), tuple(requires))


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def connect_unary(
    socket_path: Path,
    service: str,
    method: str,
    request: Mapping[str, object] | None = None,
    *,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Make one ConnectRPC unary JSON call over a Unix-domain socket."""

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
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
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


def probe_component(socket_path: Path, *, timeout: float = 2.0) -> tuple[str, str]:
    """Return the component's health state and its concrete diagnostic."""

    try:
        response = connect_unary(
            socket_path,
            COMPONENT_INTERFACE,
            "Health",
            timeout=timeout,
        )
    except CycloError as exc:
        return "unreachable", str(exc)
    status = response.get("status")
    if status == "HEALTH_STATUS_READY":
        return "ready", ""
    if status == "HEALTH_STATUS_NOT_READY":
        message = response.get("message")
        detail = " ".join(message.split()) if isinstance(message, str) else ""
        if len(detail) > 1024:
            detail = detail[:1021] + "..."
        return "not-ready", detail or "component reported not ready"
    return "not-ready", f"invalid Component.Health status: {status!r}"


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    read_only: bool = False
    type: str = "bind"


@dataclass(frozen=True)
class Component:
    """One concrete host-side component instance."""

    name: str
    declaration: Declaration
    source: Path
    build_context: Path
    image: str
    container: str
    system: str
    arguments: tuple[str, ...]
    mounts: tuple[Mount, ...]
    network: str
    socket_path: Path
    component_class: str = "provider"
    preserve_volumes: bool = False

    @property
    def kind(self) -> str:
        return self.declaration.name


@dataclass(frozen=True)
class ComponentStatus:
    """Observable facts about exactly one component."""

    name: str
    kind: str
    image_id: str | None
    container_id: str | None
    running: bool
    container_state: str
    engine_health: str
    current: bool
    health: str
    error: str = ""

    @property
    def works(self) -> bool:
        return bool(
            not self.error
            and self.container_id
            and self.running
            and self.current
            and self.engine_health == "healthy"
            and self.health == "ready"
        )
