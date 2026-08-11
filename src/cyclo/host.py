from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .dcomp_system import Endpoint, PROVIDER_SERVICE
from .errors import CycloError
from .resources import components_root


MAX_HOST_CONFIG_BYTES = 1024 * 1024
OPENAI_COMPONENT_NAME = "openai"
OPENAI_DEFAULT_BIND = "127.0.0.1"
OPENAI_DEFAULT_PORT = 8080
OPENAI_COMPONENT_SYNTAX = "component openai [bind=IPV4] [port=PORT]"
BUNDLED_PROVIDER_SOURCES = frozenset({"pooler"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SERVICE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


@dataclass(frozen=True)
class ComponentContract:
    image: str
    inputs: tuple[Endpoint, ...]
    outputs: tuple[Endpoint, ...]


@dataclass(frozen=True)
class Binding:
    input: str
    component: str
    output: str


@dataclass(frozen=True)
class Provider:
    name: str
    source: Path
    context: Path
    contract: ComponentContract
    bindings: tuple[Binding, ...]
    arguments: tuple[str, ...]
    line: int

    @property
    def provider_output(self) -> str:
        outputs = [
            endpoint.name
            for endpoint in self.contract.outputs
            if endpoint.service == PROVIDER_SERVICE
        ]
        if len(outputs) != 1:
            raise CycloError(
                f"provider {self.name!r} must expose exactly one "
                f"{PROVIDER_SERVICE} output"
            )
        return outputs[0]


@dataclass(frozen=True)
class HostComponent:
    """A bundled terminal component enabled by host configuration."""

    name: str
    bind: str
    port: int
    line: int


@dataclass(frozen=True)
class Host:
    path: Path
    providers: tuple[Provider, ...]
    generation: str
    components: tuple[HostComponent, ...] = ()

    @property
    def outer_component(self) -> str:
        return self.providers[-1].name if self.providers else "gateway"

    @property
    def outer_output(self) -> str:
        return (
            self.providers[-1].provider_output
            if self.providers
            else "provider"
        )


def _content(line: str) -> str:
    marker = re.search(r"(?:^|\s)#", line)
    return (line if marker is None else line[: marker.start()]).strip()


def _read_regular_file(
    path: Path,
    *,
    label: str,
    missing_ok: bool = False,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CycloError(f"{label} is not a regular file: {path}")
        chunks: list[bytes] = []
        remaining = MAX_HOST_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    except FileNotFoundError:
        if missing_ok:
            return b""
        raise CycloError(f"{label} not found: {path}")
    except CycloError:
        raise
    except OSError as exc:
        raise CycloError(f"cannot read {label} {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > MAX_HOST_CONFIG_BYTES:
        raise CycloError(f"{label} is too large: {path}")
    return content


def _decode(raw: bytes, path: Path, label: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CycloError(f"{label} is not UTF-8: {path}") from exc
    if any(
        character not in "\n\r"
        and (ord(character) < 0x20 or ord(character) == 0x7F)
        for character in text
    ):
        raise CycloError(f"{label} contains a control character: {path}")
    return text.replace("\r\n", "\n")


def _canonical_directory(path: Path, *, source: Path, line: int) -> Path:
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise CycloError(f"{source}:{line}: directory not found: {path}") from exc
    if not canonical.is_dir():
        raise CycloError(f"{source}:{line}: not a directory: {canonical}")
    if any(character.isspace() for character in str(canonical)):
        raise CycloError(
            f"{source}:{line}: component paths cannot contain whitespace: "
            f"{canonical}"
        )
    return canonical


def load_component(path: Path) -> ComponentContract:
    descriptor = path / "component.dcomp"
    raw = _read_regular_file(descriptor, label="DComp descriptor")
    text = _decode(raw, descriptor, "DComp descriptor")
    image: str | None = None
    inputs: list[Endpoint] = []
    outputs: list[Endpoint] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        content = _content(raw_line)
        if not content:
            continue
        fields = content.split()
        directive = fields[0]
        if directive == "docker":
            if len(fields) != 2 or image is not None:
                raise CycloError(
                    f"{descriptor}:{line_number}: expected one docker IMAGE"
                )
            image = fields[1]
        elif directive in {"input", "output"}:
            if (
                len(fields) != 3
                or not _SERVICE_RE.fullmatch(fields[1])
                or not _NAME_RE.fullmatch(fields[2])
            ):
                raise CycloError(
                    f"{descriptor}:{line_number}: expected "
                    f"{directive} PROTOBUF_SERVICE LOCAL_NAME"
                )
            endpoint = Endpoint(fields[1], fields[2])
            selected = inputs if directive == "input" else outputs
            if any(item.name == endpoint.name for item in selected):
                raise CycloError(
                    f"{descriptor}:{line_number}: duplicate {directive} "
                    f"{endpoint.name!r}"
                )
            selected.append(endpoint)
        else:
            raise CycloError(
                f"{descriptor}:{line_number}: unknown directive {directive!r}"
            )
    if image is None:
        raise CycloError(f"{descriptor}:1: missing docker IMAGE")
    contract = ComponentContract(image, tuple(inputs), tuple(outputs))
    if len(
        [
            endpoint
            for endpoint in contract.outputs
            if endpoint.service == PROVIDER_SERVICE
        ]
    ) != 1:
        raise CycloError(
            f"{descriptor}: provider components must expose exactly one "
            f"{PROVIDER_SERVICE} output"
        )
    return contract


@dataclass(frozen=True)
class _UnboundProvider:
    name: str
    source: Path
    context: Path
    contract: ComponentContract
    settings: tuple[str, ...]
    arguments: tuple[str, ...]
    line: int


def _provider_source(
    selected: Path,
    line_number: int,
    source_token: str,
) -> tuple[Path, Path, bool]:
    if source_token in BUNDLED_PROVIDER_SOURCES:
        context = _canonical_directory(
            components_root(),
            source=selected,
            line=line_number,
        )
        source = _canonical_directory(
            context / source_token,
            source=selected,
            line=line_number,
        )
        return source, context, True

    if source_token == "~" or source_token.startswith("~/"):
        raise CycloError(
            f"{selected}:{line_number}: component paths do not expand '~'"
        )
    source_path = Path(source_token)
    if not source_path.is_absolute():
        source_path = selected.parent / source_path
    source = _canonical_directory(
        source_path,
        source=selected,
        line=line_number,
    )
    return source, source, False


def _host_component(
    selected: Path,
    line_number: int,
    fields: list[str],
) -> HostComponent:
    if len(fields) < 2 or fields[1] != OPENAI_COMPONENT_NAME:
        raise CycloError(
            f"{selected}:{line_number}: expected {OPENAI_COMPONENT_SYNTAX}"
        )
    bind = OPENAI_DEFAULT_BIND
    port = OPENAI_DEFAULT_PORT
    seen: set[str] = set()
    for setting in fields[2:]:
        key, separator, value = setting.partition("=")
        if key not in {"bind", "port"} or not separator or key in seen:
            raise CycloError(
                f"{selected}:{line_number}: invalid or duplicate component "
                f"setting {setting!r}; expected bind=IPV4 or port=PORT"
            )
        seen.add(key)
        if key == "bind":
            try:
                bind = str(ipaddress.IPv4Address(value))
            except ValueError as exc:
                raise CycloError(
                    f"{selected}:{line_number}: component bind must be a "
                    "literal IPv4 address"
                ) from exc
            continue
        try:
            port = int(value, 10)
        except ValueError as exc:
            raise CycloError(
                f"{selected}:{line_number}: component port must be an integer "
                "between 1 and 65535"
            ) from exc
        if not 1 <= port <= 65535:
            raise CycloError(
                f"{selected}:{line_number}: component port must be an integer "
                "between 1 and 65535"
            )
    return HostComponent(
        OPENAI_COMPONENT_NAME,
        bind,
        port,
        line_number,
    )


def load_host(path: Path) -> Host:
    selected = Path(os.path.abspath(path.expanduser()))
    raw = _read_regular_file(
        selected,
        label="host configuration",
        missing_ok=True,
    )
    text = _decode(raw, selected, "host configuration")

    declarations: list[_UnboundProvider] = []
    components: list[HostComponent] = []
    names = {"gateway"}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        content = _content(raw_line)
        if not content:
            continue
        fields = content.split()
        if fields[0] == "component":
            component = _host_component(selected, line_number, fields)
            if component.name in names:
                raise CycloError(
                    f"{selected}:{line_number}: duplicate component name "
                    f"{component.name!r}"
                )
            names.add(component.name)
            components.append(component)
            continue
        if fields[0] != "provider" or len(fields) < 3:
            raise CycloError(
                f"{selected}:{line_number}: expected provider NAME SOURCE "
                f"[context=PATH] INPUT=COMPONENT.OUTPUT ... [-- ARGUMENT ...] "
                f"or {OPENAI_COMPONENT_SYNTAX}"
            )
        name = fields[1]
        if not _NAME_RE.fullmatch(name) or name in names:
            raise CycloError(
                f"{selected}:{line_number}: invalid or duplicate provider "
                f"name {name!r}"
            )
        names.add(name)
        source, context, bundled = _provider_source(
            selected,
            line_number,
            fields[2],
        )
        contract = load_component(source)

        separators = [
            index for index, value in enumerate(fields) if value == "--"
        ]
        if len(separators) > 1:
            raise CycloError(
                f"{selected}:{line_number}: provider line contains more than one '--'"
            )
        boundary = separators[0] if separators else len(fields)
        settings = list(fields[3:boundary])
        arguments = tuple(fields[boundary + 1 :]) if separators else ()
        context_settings = [
            setting for setting in settings if setting.startswith("context=")
        ]
        if len(context_settings) > 1:
            raise CycloError(
                f"{selected}:{line_number}: duplicate context setting"
            )
        if context_settings:
            if bundled:
                raise CycloError(
                    f"{selected}:{line_number}: bundled provider sources "
                    "do not accept context=PATH"
                )
            context_value = context_settings[0].partition("=")[2]
            if not context_value:
                raise CycloError(
                    f"{selected}:{line_number}: context path is empty"
                )
            context_path = Path(context_value)
            if not context_path.is_absolute():
                context_path = source / context_path
            context = _canonical_directory(
                context_path,
                source=selected,
                line=line_number,
            )
            settings.remove(context_settings[0])
        try:
            source.relative_to(context)
        except ValueError as exc:
            raise CycloError(
                f"{selected}:{line_number}: component source must be inside "
                f"its build context"
            ) from exc
        declarations.append(
            _UnboundProvider(
                name,
                source,
                context,
                contract,
                tuple(settings),
                arguments,
                line_number,
            )
        )

    outputs: dict[tuple[str, str], str] = {
        ("gateway", "provider"): PROVIDER_SERVICE
    }
    for declaration in declarations:
        for endpoint in declaration.contract.outputs:
            outputs[(declaration.name, endpoint.name)] = endpoint.service

    providers: list[Provider] = []
    for declaration in declarations:
        expected = {endpoint.name: endpoint for endpoint in declaration.contract.inputs}
        bindings: dict[str, Binding] = {}
        for setting in declaration.settings:
            input_name, separator, target = setting.partition("=")
            target_component, dot, target_output = target.partition(".")
            if (
                not separator
                or not dot
                or input_name not in expected
                or input_name in bindings
                or not _NAME_RE.fullmatch(target_component)
                or not _NAME_RE.fullmatch(target_output)
            ):
                raise CycloError(
                    f"{selected}:{declaration.line}: invalid or duplicate "
                    f"interface binding {setting!r}; expected "
                    f"INPUT=COMPONENT.OUTPUT"
                )
            service = outputs.get((target_component, target_output))
            if service is None:
                raise CycloError(
                    f"{selected}:{declaration.line}: binding {setting!r} "
                    f"targets an unknown output"
                )
            if service != expected[input_name].service:
                raise CycloError(
                    f"{selected}:{declaration.line}: binding {setting!r} "
                    f"provides {service}, expected {expected[input_name].service}"
                )
            bindings[input_name] = Binding(
                input_name,
                target_component,
                target_output,
            )
        missing = sorted(set(expected) - set(bindings))
        if missing:
            raise CycloError(
                f"{selected}:{declaration.line}: missing binding(s): "
                + ", ".join(
                    f"{name}=COMPONENT.OUTPUT" for name in missing
                )
            )
        providers.append(
            Provider(
                declaration.name,
                declaration.source,
                declaration.context,
                declaration.contract,
                tuple(bindings[name] for name in sorted(bindings)),
                declaration.arguments,
                declaration.line,
            )
        )

    return Host(
        path=selected,
        providers=tuple(providers),
        generation=hashlib.sha256(raw).hexdigest(),
        components=tuple(components),
    )
