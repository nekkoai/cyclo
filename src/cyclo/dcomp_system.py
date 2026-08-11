from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .errors import CycloError


PROVIDER_SERVICE = "cyclo.provider.v1.Provider"
DCOMP_COMPONENT_PORT = 50051

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SERVICE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


def _name(value: str, label: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise CycloError(f"invalid DComp {label} name: {value!r}")
    return value


def _service(value: str) -> str:
    if not _SERVICE_RE.fullmatch(value):
        raise CycloError(f"invalid protobuf service name: {value!r}")
    return value


def _token(value: str, label: str) -> str:
    if (
        not value
        or "#" in value
        or any(character.isspace() for character in value)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CycloError(
            f"{label} cannot be represented in a DComp file: {value!r}"
        )
    return value


def component_name(kind: str, identifier: str) -> str:
    """Return a stable DComp name without trusting a Cyclo display identifier."""

    _name(kind, "component kind")
    readable = re.sub(r"[^a-z0-9-]+", "-", identifier.lower()).strip("-")
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:10]
    room = 63 - len(kind) - len(digest) - 2
    stem = (readable or "instance")[:room].rstrip("-") or "instance"
    return _name(f"{kind}-{stem}-{digest}", "component")


@dataclass(frozen=True, order=True)
class Endpoint:
    service: str
    name: str

    def validate(self) -> None:
        _service(self.service)
        _name(self.name, "endpoint")


@dataclass(frozen=True, order=True)
class Bind:
    source: Path
    target: str
    read_only: bool

    def validate(self) -> None:
        source = self.source.expanduser()
        try:
            canonical = source.resolve(strict=True)
        except OSError as exc:
            raise CycloError(f"DComp bind source does not exist: {source}") from exc
        if canonical != source:
            raise CycloError(f"DComp bind source is not canonical: {source}")
        _container_path(self.target)


@dataclass(frozen=True, order=True)
class Volume:
    name: str
    target: str
    read_only: bool = False

    def validate(self) -> None:
        _name(self.name, "volume")
        _container_path(self.target)


@dataclass(frozen=True, order=True)
class PublishedPort:
    host_ip: str
    host_port: int
    container_port: int
    protocol: str = "tcp"

    def validate(self) -> None:
        if self.protocol not in {"tcp", "udp"}:
            raise CycloError(f"invalid published-port protocol: {self.protocol!r}")
        _token(self.host_ip, "published host address")
        if not 0 <= self.host_port <= 65535:
            raise CycloError("published host port must be between 0 and 65535")
        if not 1 <= self.container_port <= 65535:
            raise CycloError("published container port must be between 1 and 65535")


def _container_path(value: str) -> str:
    _token(value, "container path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or value == "/":
        raise CycloError(f"invalid DComp container path: {value!r}")
    return value


@dataclass(frozen=True)
class Component:
    name: str
    image: str
    inputs: tuple[Endpoint, ...] = ()
    outputs: tuple[Endpoint, ...] = ()
    binds: tuple[Bind, ...] = ()
    volumes: tuple[Volume, ...] = ()
    arguments: tuple[str, ...] = ()
    ports: tuple[PublishedPort, ...] = ()
    egress: bool = False

    def validate(self) -> None:
        _name(self.name, "component")
        _token(self.image, "Docker image")
        for endpoint in (*self.inputs, *self.outputs):
            endpoint.validate()
        if len({item.name for item in self.inputs}) != len(self.inputs):
            raise CycloError(f"component {self.name!r} has duplicate inputs")
        if len({item.name for item in self.outputs}) != len(self.outputs):
            raise CycloError(f"component {self.name!r} has duplicate outputs")
        for bind in self.binds:
            bind.validate()
        for volume in self.volumes:
            volume.validate()
        targets = [
            PurePosixPath(item.target) for item in (*self.binds, *self.volumes)
        ]
        for index, left in enumerate(targets):
            for right in targets[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise CycloError(
                        f"component {self.name!r} has overlapping mount targets "
                        f"{str(left)!r} and {str(right)!r}"
                    )
        for argument in self.arguments:
            _token(argument, "component argument")
        for port in self.ports:
            port.validate()
        if self.ports and not self.egress:
            raise CycloError(
                f"component {self.name!r} publishes a port without egress"
            )


@dataclass(frozen=True, order=True)
class Link:
    consumer: str
    input: str
    provider: str
    output: str

    def validate_names(self) -> None:
        for value, label in (
            (self.consumer, "consumer"),
            (self.input, "input"),
            (self.provider, "provider"),
            (self.output, "output"),
        ):
            _name(value, label)


@dataclass(frozen=True)
class System:
    name: str
    components: tuple[Component, ...]
    links: tuple[Link, ...]

    def validate(self) -> None:
        _name(self.name, "system")
        if not self.components:
            raise CycloError("a DComp system must contain at least one component")
        by_name: dict[str, Component] = {}
        for component in self.components:
            component.validate()
            if component.name in by_name:
                raise CycloError(
                    f"duplicate DComp component: {component.name!r}"
                )
            by_name[component.name] = component

        linked_inputs: set[tuple[str, str]] = set()
        for link in self.links:
            link.validate_names()
            consumer = by_name.get(link.consumer)
            provider = by_name.get(link.provider)
            if consumer is None or provider is None:
                raise CycloError(
                    f"DComp link references an unknown component: {link}"
                )
            input_endpoint = next(
                (item for item in consumer.inputs if item.name == link.input),
                None,
            )
            output_endpoint = next(
                (item for item in provider.outputs if item.name == link.output),
                None,
            )
            if input_endpoint is None or output_endpoint is None:
                raise CycloError(f"DComp link references an unknown endpoint: {link}")
            if input_endpoint.service != output_endpoint.service:
                raise CycloError(
                    f"DComp link service mismatch: {link.consumer}.{link.input} "
                    f"requires {input_endpoint.service}, but "
                    f"{link.provider}.{link.output} provides "
                    f"{output_endpoint.service}"
                )
            key = (link.consumer, link.input)
            if key in linked_inputs:
                raise CycloError(
                    f"DComp input is linked more than once: "
                    f"{link.consumer}.{link.input}"
                )
            linked_inputs.add(key)

        expected_inputs = {
            (component.name, endpoint.name)
            for component in self.components
            for endpoint in component.inputs
        }
        missing = sorted(expected_inputs - linked_inputs)
        if missing:
            raise CycloError(
                "unlinked DComp input(s): "
                + ", ".join(f"{component}.{endpoint}" for component, endpoint in missing)
            )

    @property
    def digest(self) -> str:
        self.validate()
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class MaterializedSystem:
    path: Path
    digest: str
    component_names: tuple[str, ...]


@dataclass(frozen=True)
class Materializer:
    root: Path

    def materialize(self, system: System) -> MaterializedSystem:
        """Atomically select one system file and retain only its descriptors."""

        system.validate()
        self._prepare_root()
        digest = system.digest
        descriptor_paths: dict[str, Path] = {}
        for component in sorted(system.components, key=lambda item: item.name):
            content = self._render_component(component)
            contract_digest = hashlib.sha256(content).hexdigest()[:16]
            directory = (
                self.root
                / "descriptors"
                / f"{component.name}-{contract_digest}"
            )
            self._write_descriptor(directory, content)
            descriptor_paths[component.name] = directory

        selected = self.root / "system.dcomp"
        self._replace_file(
            selected,
            self._render_system(
                system,
                digest=digest,
                descriptors=descriptor_paths,
            ),
        )
        self._remove_unused_descriptors(set(descriptor_paths.values()))
        return MaterializedSystem(
            selected,
            digest,
            tuple(sorted(component.name for component in system.components)),
        )

    def _prepare_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptors = self.root / "descriptors"
            descriptors.mkdir(exist_ok=True)
            self._sync_directory(descriptors)
            self._sync_directory(self.root)
        except OSError as exc:
            raise CycloError(
                f"cannot prepare generated DComp directory {self.root}: {exc}"
            ) from exc

    def _render_component(self, component: Component) -> bytes:
        lines = [f"docker {_token(component.image, 'Docker image')}"]
        lines.extend(
            f"input {endpoint.service} {endpoint.name}"
            for endpoint in sorted(component.inputs)
        )
        lines.extend(
            f"output {endpoint.service} {endpoint.name}"
            for endpoint in sorted(component.outputs)
        )
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _render_system(
        self,
        system: System,
        *,
        digest: str,
        descriptors: dict[str, Path],
    ) -> bytes:
        by_name = {component.name: component for component in system.components}
        lines = [
            f"# generated by Cyclo; composition {digest}",
            f"system {system.name}",
            "",
        ]
        for component in sorted(system.components, key=lambda item: item.name):
            descriptor = descriptors[component.name].relative_to(self.root)
            lines.append(f"component {component.name} {descriptor}")
            for bind in sorted(component.binds):
                source = self._source_token(bind.source)
                mode = "ro" if bind.read_only else "rw"
                lines.append(
                    f"bind {component.name} {source} {bind.target} {mode}"
                )
            for volume in sorted(component.volumes):
                mode = "ro" if volume.read_only else "rw"
                lines.append(
                    f"volume {component.name} {volume.name} {volume.target} {mode}"
                )
            if component.arguments:
                arguments = " ".join(
                    _token(argument, "component argument")
                    for argument in component.arguments
                )
                lines.append(f"args {component.name} {arguments}")
            for port in sorted(component.ports):
                lines.append(
                    f"publish {component.name} {port.protocol} {port.host_ip} "
                    f"{port.host_port} {port.container_port}"
                )
            if component.egress:
                lines.append(f"egress {component.name}")
            lines.append("")
        for link in sorted(system.links):
            # Names and services have already been checked by System.validate.
            assert link.consumer in by_name and link.provider in by_name
            lines.append(
                f"link {link.consumer}.{link.input} "
                f"{link.provider}.{link.output}"
            )
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

    def _write_descriptor(self, directory: Path, content: bytes) -> None:
        path = directory / "component.dcomp"
        try:
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise CycloError(
                        f"invalid generated DComp descriptor directory: {directory}"
                    )
                if path.is_symlink() or not path.is_file():
                    raise CycloError(
                        f"invalid generated DComp descriptor: {path}"
                    )
                if path.read_bytes() != content:
                    raise CycloError(
                        f"generated DComp descriptor content mismatch: {path}"
                    )
                return
            temporary = directory.with_name(
                f".{directory.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
            )
            try:
                temporary.mkdir()
                self._write_file(temporary / "component.dcomp", content)
                self._sync_directory(temporary)
                os.replace(temporary, directory)
                self._sync_directory(directory.parent)
            finally:
                if temporary.exists():
                    (temporary / "component.dcomp").unlink(missing_ok=True)
                    temporary.rmdir()
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(
                f"cannot materialize DComp descriptor {directory}: {exc}"
            ) from exc

    def _remove_unused_descriptors(self, keep: set[Path]) -> None:
        directory = self.root / "descriptors"
        try:
            for path in directory.iterdir():
                if path in keep:
                    continue
                if (
                    path.is_symlink()
                    or not path.is_dir()
                ):
                    raise CycloError(
                        f"unexpected generated DComp descriptor entry: {path}"
                    )
                manifest = path / "component.dcomp"
                contents = tuple(path.iterdir())
                # A process death after creating a descriptor directory but
                # before publishing its one file leaves an empty directory.
                # It is an uncommitted generated object and is safe to remove.
                if not contents:
                    path.rmdir()
                    continue
                if (
                    len(contents) != 1
                    or contents[0] != manifest
                    or manifest.is_symlink()
                    or not manifest.is_file()
                ):
                    raise CycloError(
                        f"unexpected generated DComp descriptor contents: {path}"
                    )
                manifest.unlink()
                path.rmdir()
            self._sync_directory(directory)
        except CycloError:
            raise
        except OSError as exc:
            raise CycloError(
                f"cannot remove obsolete DComp descriptors: {exc}"
            ) from exc

    def _source_token(self, source: Path) -> str:
        canonical = source.resolve(strict=True)
        try:
            relative = canonical.relative_to(self.root)
        except ValueError:
            value = str(canonical)
        else:
            value = str(relative) or "."
        return _token(value, "bind source")

    def _replace_file(self, destination: Path, content: bytes) -> None:
        temporary = destination.with_name(
            f".{destination.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        )
        try:
            self._write_file(temporary, content)
            os.replace(temporary, destination)
            self._sync_directory(destination.parent)
        except OSError as exc:
            raise CycloError(
                f"cannot select generated DComp system {destination}: {exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _write_file(self, path: Path, content: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def provider_endpoint(name: str = "provider") -> Endpoint:
    return Endpoint(PROVIDER_SERVICE, name)


def inputs(
    values: Iterable[tuple[str, str]],
) -> tuple[Endpoint, ...]:
    return tuple(Endpoint(service, name) for service, name in values)
