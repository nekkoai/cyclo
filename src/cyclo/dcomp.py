from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, TextIO

from .errors import CycloError
from .state import StateStore


DCOMP_API_VERSION = 1
DCOMP_EXECUTABLE_ENV = "CYCLO_DCOMP"


@dataclass(frozen=True)
class DCompVersion:
    version: str
    api_version: int


@dataclass(frozen=True)
class DCompPublishedPort:
    protocol: str
    host_ip: str
    host_port: int
    container_port: int


@dataclass(frozen=True)
class DCompComponentStatus:
    name: str
    container_id: str
    status: str
    health: str
    exit_code: int
    problem: str
    published_ports: tuple[DCompPublishedPort, ...]


@dataclass(frozen=True)
class DCompNetworkStatus:
    key: str
    id: str
    internal: bool
    problem: str


@dataclass(frozen=True)
class DCompStatus:
    api_version: int
    name: str
    desired: bool
    operational: bool
    digest: str
    operation: str
    phase: str
    networks: tuple[DCompNetworkStatus, ...]
    components: tuple[DCompComponentStatus, ...]

    def component(self, name: str) -> DCompComponentStatus | None:
        return next(
            (component for component in self.components if component.name == name),
            None,
        )


class DCompClient:
    """The complete Cyclo boundary to the external dcomp executable."""

    def __init__(
        self,
        store: StateStore,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.state_root = store.root / "dcomp"
        self._environment = dict(
            os.environ if environment is None else environment
        )
        self.executable = _find_executable(self._environment)
        bound_endpoint = store.bound_docker_endpoint
        if bound_endpoint is not None:
            self._environment["DOCKER_HOST"] = bound_endpoint
            # A selected Docker context must not override the realm's
            # durable daemon binding.
            self._environment.pop("DOCKER_CONTEXT", None)
        self._compatible = False

    def bind_docker(self, endpoint: str) -> None:
        """Pin every later DComp command to Cyclo's durable Docker endpoint."""

        existing = self._environment.get("DOCKER_HOST")
        if existing is not None and existing != endpoint:
            raise CycloError(
                "DComp client is already bound to another Docker endpoint"
            )
        self._environment["DOCKER_HOST"] = endpoint
        self._environment.pop("DOCKER_CONTEXT", None)

    def version(self) -> DCompVersion:
        process = self._run(
            ["version", "--json"],
            action="version",
            stateful=False,
        )
        payload = _json_object(process.stdout, "dcomp version")
        version = _string(payload, "version", "dcomp version")
        api_version = _integer(payload, "api_version", "dcomp version")
        if not version:
            raise CycloError("invalid dcomp version response: version is empty")
        return DCompVersion(version=version, api_version=api_version)

    def check(self, system_file: str | os.PathLike[str]) -> None:
        self._require_compatible()
        self._run(["check", os.fspath(system_file)], action="check")

    def up(self, system_file: str | os.PathLike[str]) -> None:
        self._require_compatible()
        self._run(["up", os.fspath(system_file)], action="up")

    def status(self, name: str) -> DCompStatus:
        self._require_compatible()
        process = self._run(
            ["status", "--json", name],
            action="status",
            accepted_returncodes=(0, 1),
        )
        try:
            status = _status_from_json(process.stdout)
        except CycloError as exc:
            if process.stderr:
                detail = _command_detail(process)
                raise CycloError(
                    _command_failure("status", process.returncode, detail)
                ) from exc
            raise
        if status.name != name:
            raise CycloError(
                "invalid dcomp status response: "
                f"requested {name!r}, received {status.name!r}"
            )
        expected_returncode = 0 if status.operational else 1
        if process.returncode != expected_returncode:
            raise CycloError(
                "invalid dcomp status response: operational state and "
                "exit status disagree"
            )
        return status

    def volume(self, system: str, component: str, logical_name: str) -> str:
        """Resolve one verified DComp-owned volume to its opaque Docker name."""

        self._require_compatible()
        process = self._run(
            ["volume", "--json", system, component, logical_name],
            action="volume",
        )
        source = "dcomp volume"
        payload = _json_object(process.stdout, source)
        expected_fields = {
            "api_version",
            "system",
            "component",
            "logical_name",
            "name",
        }
        if set(payload) != expected_fields:
            missing = sorted(expected_fields - set(payload))
            unknown = sorted(set(payload) - expected_fields)
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if unknown:
                details.append(f"unknown {', '.join(unknown)}")
            raise CycloError(
                "invalid dcomp volume response: " + "; ".join(details)
            )
        api_version = _integer(payload, "api_version", source)
        if api_version != DCOMP_API_VERSION:
            raise CycloError(
                "incompatible dcomp volume API: "
                f"need {DCOMP_API_VERSION}, found {api_version}"
            )
        returned_system = _string(payload, "system", source)
        returned_component = _string(payload, "component", source)
        returned_logical_name = _string(payload, "logical_name", source)
        if (
            returned_system != system
            or returned_component != component
            or returned_logical_name != logical_name
        ):
            raise CycloError(
                "invalid dcomp volume response: requested "
                f"{system}.{component}.{logical_name}, received "
                f"{returned_system}.{returned_component}."
                f"{returned_logical_name}"
            )
        name = _string(payload, "name", source)
        if not name:
            raise CycloError("invalid dcomp volume response: name is empty")
        return name

    def restart(self, name: str, *components: str) -> None:
        self._require_compatible()
        self._run(
            ["restart", name, *components],
            action="restart",
        )

    def logs(
        self,
        name: str,
        *components: str,
        follow: bool = False,
        output: TextIO | None = None,
    ) -> None:
        self._require_compatible()
        arguments = ["logs"]
        if follow:
            arguments.append("--follow")
        arguments.append(name)
        arguments.extend(components)
        self._run(
            arguments,
            action="logs",
            stdout=output,
        )

    def down(self, name: str) -> None:
        self._require_compatible()
        self._run(["down", name], action="down")

    def resume(self, name: str) -> None:
        self._require_compatible()
        self._run(["resume", name], action="resume")

    def abort(self, name: str) -> None:
        self._require_compatible()
        self._run(["abort", name], action="abort")

    def _require_compatible(self) -> None:
        if self._compatible:
            return
        version = self.version()
        if version.api_version != DCOMP_API_VERSION:
            raise CycloError(
                "incompatible dcomp machine API: "
                f"need {DCOMP_API_VERSION}, found {version.api_version} "
                f"in dcomp {version.version}"
            )
        self._compatible = True

    def _run(
        self,
        arguments: list[str],
        *,
        action: str,
        stateful: bool = True,
        accepted_returncodes: tuple[int, ...] = (0,),
        stdout: TextIO | None | int = subprocess.PIPE,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable]
        if stateful:
            command.extend(["--state-root", os.fspath(self.state_root)])
        command.extend(arguments)
        try:
            process = subprocess.run(
                command,
                check=False,
                text=True,
                stdin=None,
                stdout=stdout,
                stderr=subprocess.PIPE,
                env=self._environment,
            )
        except FileNotFoundError as exc:
            raise CycloError(
                f"cannot run dcomp {action}: executable is unavailable"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CycloError(f"dcomp {action} timed out") from exc
        except ValueError as exc:
            raise CycloError(f"cannot run dcomp {action}: {exc}") from exc
        except OSError as exc:
            raise CycloError(f"cannot run dcomp {action}: {exc}") from exc
        if process.returncode not in accepted_returncodes:
            detail = _command_detail(process)
            raise CycloError(
                _command_failure(action, process.returncode, detail)
            )
        return process


def _find_executable(environment: Mapping[str, str]) -> str:
    override = environment.get(DCOMP_EXECUTABLE_ENV)
    if override is not None and not override:
        raise CycloError(f"{DCOMP_EXECUTABLE_ENV} is empty")
    requested = override or "dcomp"
    executable = shutil.which(requested, path=environment.get("PATH"))
    if executable is None:
        if override is not None:
            raise CycloError(
                f"dcomp executable from {DCOMP_EXECUTABLE_ENV} was not found: "
                f"{override}"
            )
        raise CycloError(
            "dcomp is not installed or not on PATH; set CYCLO_DCOMP to its "
            "executable"
        )
    return os.path.realpath(executable)


def _command_detail(process: subprocess.CompletedProcess[str]) -> str:
    raw = process.stderr or process.stdout or ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("error:"):
            lines = lines[index:]
            lines[0] = lines[0].removeprefix("error:").lstrip()
            break
    return "\n".join(line for line in lines if line)[:2048]


def _command_failure(action: str, status: int, detail: str) -> str:
    message = f"dcomp {action} failed with status {status}"
    if not detail:
        return message
    indented = "\n".join(f"  {line}" for line in detail.splitlines())
    return f"{message}:\n{indented}"


def _json_object(raw: str | None, source: str) -> dict[str, object]:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise CycloError(
            f"invalid {source} response: expected one JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise CycloError(f"invalid {source} response: expected one JSON object")
    return value


def _string(value: Mapping[str, object], key: str, source: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise CycloError(f"invalid {source} response: {key} must be a string")
    return result


def _integer(value: Mapping[str, object], key: str, source: str) -> int:
    result = value.get(key)
    if type(result) is not int:
        raise CycloError(f"invalid {source} response: {key} must be an integer")
    return result


def _boolean(value: Mapping[str, object], key: str, source: str) -> bool:
    result = value.get(key)
    if type(result) is not bool:
        raise CycloError(f"invalid {source} response: {key} must be a boolean")
    return result


def _array(
    value: Mapping[str, object],
    key: str,
    source: str,
) -> list[object]:
    result = value.get(key)
    if not isinstance(result, list):
        raise CycloError(f"invalid {source} response: {key} must be an array")
    return result


def _nested_object(value: object, source: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CycloError(f"invalid {source} response: expected an object")
    return value


def _status_from_json(raw: str | None) -> DCompStatus:
    source = "dcomp status"
    payload = _json_object(raw, source)
    api_version = _integer(payload, "api_version", source)
    if api_version != DCOMP_API_VERSION:
        raise CycloError(
            "incompatible dcomp status API: "
            f"need {DCOMP_API_VERSION}, found {api_version}"
        )

    networks = tuple(
        _network_from_object(item, index)
        for index, item in enumerate(_array(payload, "networks", source))
    )
    components = tuple(
        _component_from_object(item, index)
        for index, item in enumerate(_array(payload, "components", source))
    )
    _require_unique((network.key for network in networks), "network keys")
    _require_unique((component.name for component in components), "component names")
    return DCompStatus(
        api_version=api_version,
        name=_string(payload, "name", source),
        desired=_boolean(payload, "desired", source),
        operational=_boolean(payload, "operational", source),
        digest=_string(payload, "digest", source),
        operation=_string(payload, "operation", source),
        phase=_string(payload, "phase", source),
        networks=networks,
        components=components,
    )


def _network_from_object(value: object, index: int) -> DCompNetworkStatus:
    source = f"dcomp status networks[{index}]"
    payload = _nested_object(value, source)
    return DCompNetworkStatus(
        key=_string(payload, "key", source),
        id=_string(payload, "id", source),
        internal=_boolean(payload, "internal", source),
        problem=_string(payload, "problem", source),
    )


def _component_from_object(value: object, index: int) -> DCompComponentStatus:
    source = f"dcomp status components[{index}]"
    payload = _nested_object(value, source)
    ports = tuple(
        _published_port_from_object(item, index, port_index)
        for port_index, item in enumerate(
            _array(payload, "published_ports", source)
        )
    )
    return DCompComponentStatus(
        name=_string(payload, "name", source),
        container_id=_string(payload, "container_id", source),
        status=_string(payload, "status", source),
        health=_string(payload, "health", source),
        exit_code=_integer(payload, "exit_code", source),
        problem=_string(payload, "problem", source),
        published_ports=ports,
    )


def _published_port_from_object(
    value: object,
    component_index: int,
    port_index: int,
) -> DCompPublishedPort:
    source = (
        f"dcomp status components[{component_index}]"
        f".published_ports[{port_index}]"
    )
    payload = _nested_object(value, source)
    protocol = _string(payload, "protocol", source)
    if protocol not in {"tcp", "udp"}:
        raise CycloError(
            f"invalid {source} response: protocol must be tcp or udp"
        )
    host_port = _port(payload, "host_port", source, allow_zero=True)
    container_port = _port(payload, "container_port", source, allow_zero=False)
    return DCompPublishedPort(
        protocol=protocol,
        host_ip=_string(payload, "host_ip", source),
        host_port=host_port,
        container_port=container_port,
    )


def _port(
    value: Mapping[str, object],
    key: str,
    source: str,
    *,
    allow_zero: bool,
) -> int:
    port = _integer(value, key, source)
    minimum = 0 if allow_zero else 1
    if not minimum <= port <= 65535:
        raise CycloError(f"invalid {source} response: {key} is out of range")
    return port


def _require_unique(values: Iterable[str], description: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise CycloError(
                f"invalid dcomp status response: duplicate {description[:-1]} "
                f"{value!r}"
            )
        seen.add(value)
