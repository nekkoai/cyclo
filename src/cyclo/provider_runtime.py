from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import CycloError
from .host_config import PROVIDER_PREFIX_RE, RESERVED_PROVIDER_PREFIXES


PROVIDER_PROTOCOL_VERSION = "1"

PROVIDER_OWNERSHIP_LABEL = "cyclo.provider"
PROVIDER_OWNERSHIP_VALUE = "1"
PROVIDER_SYSTEM_LABEL = "cyclo.provider-system"
PROVIDER_PREFIX_LABEL = "cyclo.provider-prefix"
PROVIDER_RESOURCE_LABEL = "cyclo.provider-resource"
PROVIDER_SOURCE_FINGERPRINT_LABEL = "cyclo.provider-source-fingerprint"
PROVIDER_CONFIG_FINGERPRINT_LABEL = "cyclo.provider-config-fingerprint"

CONTAINER_UPSTREAM_TOKEN = Path("/run/secrets/cyclo-upstream-token")
CONTAINER_PROVIDER_TOKEN = Path("/run/secrets/cyclo-provider-token")
CONTAINER_RUNTIME_SOCKET_DIR = Path("/run/cyclo/runtime")
CONTAINER_RUNTIME_SOCKET = CONTAINER_RUNTIME_SOCKET_DIR / "runtime.sock"
CONTAINER_PROVIDER_SOCKET_DIR = Path("/run/cyclo/self")
CONTAINER_PROVIDER_SOCKET = CONTAINER_PROVIDER_SOCKET_DIR / "provider.sock"

PROVIDER_MEMORY_LIMIT = "512m"
PROVIDER_CPU_LIMIT = "2"

_TOKEN_LIMIT = 64 * 1024
_MISSING_CONTAINER = ("no such container", "no such object")
_MISSING_IMAGE = ("no such image", "no such object")


def _validate_prefix(prefix: str) -> None:
    if (
        not isinstance(prefix, str)
        or not PROVIDER_PREFIX_RE.fullmatch(prefix)
        or prefix in RESERVED_PROVIDER_PREFIXES
    ):
        raise CycloError(f"invalid provider prefix: {prefix!r}")


def _system_id(state_root: Path) -> str:
    selected = Path(state_root).expanduser().resolve()
    return hashlib.sha256(str(selected).encode("utf-8")).hexdigest()[:12]


def _name_fragment(prefix: str) -> str:
    return f"{prefix[:24]}-{hashlib.sha256(prefix.encode('utf-8')).hexdigest()[:8]}"


@dataclass(frozen=True)
class ProviderIdentity:
    system_id: str
    prefix: str
    image: str
    container: str


def provider_identity(state_root: Path, prefix: str) -> ProviderIdentity:
    """Return deterministic, installation-scoped Docker resource names."""

    _validate_prefix(prefix)
    system_id = _system_id(state_root)
    stem = f"cyclo-provider-{system_id}-{_name_fragment(prefix)}"
    return ProviderIdentity(
        system_id=system_id,
        prefix=prefix,
        image=f"{stem}:local",
        container=stem,
    )


@dataclass(frozen=True)
class ProviderSpec:
    identity: ProviderIdentity
    source: Path
    arguments: tuple[str, ...]
    runtime_socket_dir: Path
    provider_socket_dir: Path
    upstream_token_file: Path
    provider_token_file: Path

    def __post_init__(self) -> None:
        _validate_prefix(self.identity.prefix)
        for selected, label in (
            (self.runtime_socket_dir, "provider runtime socket directory"),
            (self.provider_socket_dir, "provider socket directory"),
            (self.upstream_token_file, "upstream capability"),
            (self.provider_token_file, "provider capability"),
        ):
            if not Path(selected).is_absolute():
                raise CycloError(f"{label} must be an absolute host path: {selected}")


@dataclass(frozen=True)
class ProviderStatus:
    identity: ProviderIdentity
    source_fingerprint: str
    generation: str
    config_fingerprint: str
    image_built: bool
    container_restarted: bool
    container_id: str


@dataclass(frozen=True)
class ProviderContainerStatus:
    identity: ProviderIdentity
    image_exists: bool
    image_current: bool
    container_exists: bool
    container_running: bool
    configuration_current: bool
    container_id: str | None


def _fingerprint_path(digest: "hashlib._Hash", root: Path, path: Path) -> None:
    relative = path.relative_to(root).as_posix().encode(
        "utf-8", errors="surrogateescape"
    )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CycloError(f"cannot fingerprint provider source {path}: {exc}") from exc
    digest.update(relative)
    digest.update(b"\0")
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise CycloError(
                f"cannot read provider source symlink {path}: {exc}"
            ) from exc
        digest.update(b"symlink\0")
        digest.update(os.fsencode(target))
        digest.update(b"\0")
        return
    if not stat.S_ISREG(metadata.st_mode):
        return
    digest.update(b"file\0")
    digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
    digest.update(b"\0")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise CycloError(f"provider source changed while fingerprinting: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CycloError(f"cannot fingerprint provider source {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    digest.update(b"\0")


def provider_source_fingerprint(source: Path) -> str:
    """Hash an operator-owned Docker context without following symlinks."""

    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise CycloError(f"provider source is not a directory: {root}")
    dockerfile = root / "Dockerfile"
    try:
        dockerfile_mode = dockerfile.lstat().st_mode
    except OSError as exc:
        raise CycloError(f"provider source has no Dockerfile: {root}") from exc
    if not stat.S_ISREG(dockerfile_mode):
        raise CycloError(f"provider Dockerfile is not a regular file: {dockerfile}")

    digest = hashlib.sha256()
    digest.update(b"cyclo-provider-source-v1\0")
    try:
        for current, directories, files in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            selected_directories: list[str] = []
            symlink_directories: list[Path] = []
            for name in sorted(directories):
                if name == ".git":
                    continue
                candidate = current_path / name
                if candidate.is_symlink():
                    symlink_directories.append(candidate)
                else:
                    selected_directories.append(name)
            directories[:] = selected_directories
            for path in sorted(
                [*symlink_directories, *(current_path / name for name in files)],
                key=lambda item: item.relative_to(root).as_posix(),
            ):
                if ".git" in path.relative_to(root).parts:
                    continue
                _fingerprint_path(digest, root, path)
    except OSError as exc:
        raise CycloError(f"cannot walk provider source {root}: {exc}") from exc
    return digest.hexdigest()


def _labels(identity: ProviderIdentity, resource: str) -> tuple[str, ...]:
    return (
        f"{PROVIDER_OWNERSHIP_LABEL}={PROVIDER_OWNERSHIP_VALUE}",
        f"{PROVIDER_SYSTEM_LABEL}={identity.system_id}",
        f"{PROVIDER_PREFIX_LABEL}={identity.prefix}",
        f"{PROVIDER_RESOURCE_LABEL}={resource}",
    )


def provider_build_command(spec: ProviderSpec, source_fingerprint: str) -> list[str]:
    command = ["docker", "build", "-t", spec.identity.image]
    for label in _labels(spec.identity, spec.identity.image):
        command.extend(["--label", label])
    command.extend(
        [
            "--label",
            f"{PROVIDER_SOURCE_FINGERPRINT_LABEL}={source_fingerprint}",
            "-f",
            str(spec.source.resolve() / "Dockerfile"),
            str(spec.source.resolve()),
        ]
    )
    return command


def _bind(source: Path, target: Path, *, readonly: bool) -> str:
    if "," in str(source):
        raise CycloError(f"Docker bind source cannot contain a comma: {source}")
    option = ",readonly" if readonly else ""
    return f"type=bind,src={source},dst={target}{option}"


def _require_socket_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CycloError(f"cannot inspect {label} {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CycloError(f"{label} is symlinked or is not a directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o777:
        raise CycloError(f"{label} must have mode 0777: {path}")


def provider_config_fingerprint(
    spec: ProviderSpec, source_fingerprint: str
) -> str:
    _require_socket_directory(
        spec.runtime_socket_dir, "provider runtime socket directory"
    )
    _require_socket_directory(spec.provider_socket_dir, "provider socket directory")
    data = {
        "protocol": PROVIDER_PROTOCOL_VERSION,
        "source": source_fingerprint,
        "prefix": spec.identity.prefix,
        "image": spec.identity.image,
        "arguments": list(spec.arguments),
        "runtime_socket_dir": str(spec.runtime_socket_dir),
        "provider_socket_dir": str(spec.provider_socket_dir),
        "runtime_socket": str(CONTAINER_RUNTIME_SOCKET),
        "provider_socket": str(CONTAINER_PROVIDER_SOCKET),
        "upstream_token_sha256": _token_digest(spec.upstream_token_file),
        "provider_token_sha256": _token_digest(spec.provider_token_file),
        "memory": PROVIDER_MEMORY_LIMIT,
        "cpus": PROVIDER_CPU_LIMIT,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def provider_generation(
    identity: ProviderIdentity,
    arguments: Sequence[str],
    source_fingerprint: str,
) -> str:
    """Return the public implementation generation used for registration."""

    data = {
        "protocol": PROVIDER_PROTOCOL_VERSION,
        "source": source_fingerprint,
        "prefix": identity.prefix,
        "arguments": list(arguments),
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def provider_run_command(
    spec: ProviderSpec,
    *,
    generation: str,
    config_fingerprint: str,
) -> list[str]:
    _require_socket_directory(
        spec.runtime_socket_dir, "provider runtime socket directory"
    )
    _require_socket_directory(spec.provider_socket_dir, "provider socket directory")
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        spec.identity.container,
    ]
    for label in _labels(spec.identity, spec.identity.container):
        command.extend(["--label", label])
    command.extend(
        [
            "--label",
            f"{PROVIDER_CONFIG_FINGERPRINT_LABEL}={config_fingerprint}",
            "--restart",
            "unless-stopped",
            "--stop-timeout",
            "10",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "256",
            "--ulimit",
            "nofile=1024:1024",
            "--memory",
            PROVIDER_MEMORY_LIMIT,
            "--memory-swap",
            PROVIDER_MEMORY_LIMIT,
            "--cpus",
            PROVIDER_CPU_LIMIT,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--network",
            "none",
            "-e",
            f"CYCLO_PROVIDER_PROTOCOL={PROVIDER_PROTOCOL_VERSION}",
            "-e",
            f"CYCLO_PROVIDER_PREFIX={spec.identity.prefix}",
            "-e",
            f"CYCLO_PROVIDER_GENERATION={generation}",
            "-e",
            f"CYCLO_PROVIDER_RUNTIME_SOCKET={CONTAINER_RUNTIME_SOCKET}",
            "-e",
            f"CYCLO_PROVIDER_SOCKET={CONTAINER_PROVIDER_SOCKET}",
            "-e",
            f"CYCLO_UPSTREAM_TOKEN_FILE={CONTAINER_UPSTREAM_TOKEN}",
            "-e",
            f"CYCLO_PROVIDER_TOKEN_FILE={CONTAINER_PROVIDER_TOKEN}",
            "--mount",
            _bind(
                spec.runtime_socket_dir,
                CONTAINER_RUNTIME_SOCKET_DIR,
                readonly=True,
            ),
            "--mount",
            _bind(
                spec.provider_socket_dir,
                CONTAINER_PROVIDER_SOCKET_DIR,
                readonly=False,
            ),
            "--mount",
            _bind(
                spec.upstream_token_file,
                CONTAINER_UPSTREAM_TOKEN,
                readonly=True,
            ),
            "--mount",
            _bind(
                spec.provider_token_file,
                CONTAINER_PROVIDER_TOKEN,
                readonly=True,
            ),
            spec.identity.image,
            *spec.arguments,
        ]
    )
    return command


def _token_digest(path: Path) -> str:
    selected = Path(path)
    try:
        metadata = selected.lstat()
    except OSError as exc:
        raise CycloError(f"cannot read provider capability file {selected}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CycloError(f"provider capability path is not a regular file: {selected}")
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise CycloError(f"provider capability file must have mode 0444: {selected}")
    try:
        parent_metadata = selected.parent.lstat()
    except OSError as exc:
        raise CycloError(
            f"cannot inspect provider capability directory {selected.parent}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise CycloError(
            f"provider capability directory must have mode 0700: {selected.parent}"
        )
    descriptor = -1
    try:
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise CycloError(f"provider capability changed while reading: {selected}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            value = stream.read(_TOKEN_LIMIT + 1)
    except OSError as exc:
        raise CycloError(f"cannot read provider capability file {selected}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value or len(value) > _TOKEN_LIMIT:
        raise CycloError(f"provider capability file has an invalid size: {selected}")
    return hashlib.sha256(value).hexdigest()


class ProviderRuntime:
    """Fail-closed lifecycle for networkless, Unix-socket provider containers."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.system_id = _system_id(self.state_root)

    def identity(self, prefix: str) -> ProviderIdentity:
        return provider_identity(self.state_root, prefix)

    def _run(
        self,
        command: Sequence[str],
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.run(
                list(command),
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CycloError("Docker is not installed or not on PATH") from exc
        if check and process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            raise CycloError(
                f"Docker command failed ({process.returncode}): "
                f"{detail or ' '.join(command)}"
            )
        return process

    def _inspect(
        self,
        kind: str,
        identifier: str,
        *,
        missing: tuple[str, ...],
    ) -> dict[str, object] | None:
        process = self._run(
            ["docker", kind, "inspect", identifier], capture=True, check=False
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            if any(marker in detail.lower() for marker in missing):
                return None
            raise CycloError(
                f"cannot inspect Docker {kind} {identifier}: "
                f"{detail or 'unknown Docker error'}"
            )
        try:
            document = json.loads(process.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CycloError(f"cannot inspect Docker {kind}: {identifier}") from exc
        if (
            not isinstance(document, list)
            or len(document) != 1
            or not isinstance(document[0], dict)
        ):
            raise CycloError(f"cannot inspect Docker {kind}: {identifier}")
        return document[0]

    def _inspect_container(self, identifier: str) -> dict[str, object] | None:
        return self._inspect("container", identifier, missing=_MISSING_CONTAINER)

    def _inspect_image(self, identifier: str) -> dict[str, object] | None:
        return self._inspect("image", identifier, missing=_MISSING_IMAGE)

    @staticmethod
    def _resource_id(
        info: Mapping[str, object], *, kind: str, name: str
    ) -> str:
        identifier = info.get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise CycloError(f"cannot inspect Docker {kind} {name}: missing resource ID")
        return identifier

    @staticmethod
    def _resource_name(
        info: Mapping[str, object], *, kind: str, requested: str
    ) -> str:
        value = info.get("Name")
        if kind == "container" and isinstance(value, str) and value.startswith("/"):
            value = value[1:]
        if not isinstance(value, str) or not value:
            if kind == "image":
                return requested
            raise CycloError(
                f"cannot inspect Docker {kind} {requested}: missing resource name"
            )
        return value

    @staticmethod
    def _resource_labels(
        info: Mapping[str, object], *, kind: str
    ) -> Mapping[str, object]:
        if kind in {"container", "image"}:
            config = info.get("Config")
            labels = config.get("Labels") if isinstance(config, Mapping) else None
        else:
            labels = info.get("Labels")
        return labels if isinstance(labels, Mapping) else {}

    def _require_owned(
        self,
        info: Mapping[str, object],
        identity: ProviderIdentity,
        *,
        kind: str,
        name: str,
    ) -> Mapping[str, object]:
        actual_name = self._resource_name(info, kind=kind, requested=name)
        labels = self._resource_labels(info, kind=kind)
        if (
            actual_name != name
            or labels.get(PROVIDER_OWNERSHIP_LABEL) != PROVIDER_OWNERSHIP_VALUE
            or labels.get(PROVIDER_SYSTEM_LABEL) != identity.system_id
            or labels.get(PROVIDER_PREFIX_LABEL) != identity.prefix
            or labels.get(PROVIDER_RESOURCE_LABEL) != name
        ):
            raise CycloError(
                f"Docker {kind} name is already owned outside this Cyclo "
                f"provider: {name}"
            )
        self._resource_id(info, kind=kind, name=name)
        return labels

    def _owned(
        self, identity: ProviderIdentity, *, kind: str, name: str
    ) -> dict[str, object] | None:
        inspect = {
            "container": self._inspect_container,
            "image": self._inspect_image,
        }[kind]
        info = inspect(name)
        if info is not None:
            self._require_owned(info, identity, kind=kind, name=name)
        return info

    def build(self, spec: ProviderSpec) -> str:
        """Build exactly one provider image; never starts or replaces a container."""

        if spec.identity.system_id != self.system_id:
            raise CycloError("provider identity belongs to a different Cyclo installation")
        source_fingerprint = provider_source_fingerprint(spec.source)
        self._run(provider_build_command(spec, source_fingerprint))
        built = self._owned(spec.identity, kind="image", name=spec.identity.image)
        if built is None:
            raise CycloError(f"provider image disappeared after build: {spec.identity.image}")
        labels = self._resource_labels(built, kind="image")
        if labels.get(PROVIDER_SOURCE_FINGERPRINT_LABEL) != source_fingerprint:
            raise CycloError(
                f"provider image has the wrong source fingerprint: {spec.identity.image}"
            )
        self._require_image_entrypoint(built, spec.identity.image)
        return source_fingerprint

    def require_current_image(self, spec: ProviderSpec) -> str:
        source_fingerprint = provider_source_fingerprint(spec.source)
        info = self._owned(spec.identity, kind="image", name=spec.identity.image)
        labels = self._resource_labels(info, kind="image") if info is not None else {}
        if (
            info is None
            or labels.get(PROVIDER_SOURCE_FINGERPRINT_LABEL) != source_fingerprint
        ):
            raise CycloError(
                f"provider image is missing or stale for {spec.identity.prefix!r}; "
                f"run `cyclo provider build {spec.identity.prefix}`"
            )
        self._require_image_entrypoint(info, spec.identity.image)
        return source_fingerprint

    def _launch(
        self,
        spec: ProviderSpec,
        *,
        source_fingerprint: str,
    ) -> ProviderStatus:
        generation = provider_generation(
            spec.identity, spec.arguments, source_fingerprint
        )
        config_fingerprint = provider_config_fingerprint(spec, source_fingerprint)
        self._clear_provider_socket(spec)
        self._run(
            provider_run_command(
                spec,
                generation=generation,
                config_fingerprint=config_fingerprint,
            ),
            capture=True,
        )
        try:
            started = self._owned(
                spec.identity, kind="container", name=spec.identity.container
            )
            if started is None:
                raise CycloError(
                    "provider container disappeared after start: "
                    f"{spec.identity.container}"
                )
            if (
                not self._container_running(started)
                or not self._container_has_no_network(started)
            ):
                raise CycloError(
                    "provider container failed to start without a network: "
                    f"{spec.identity.container}"
                )
            labels = self._resource_labels(started, kind="container")
            if labels.get(PROVIDER_CONFIG_FINGERPRINT_LABEL) != config_fingerprint:
                raise CycloError(
                    "provider container has the wrong configuration: "
                    f"{spec.identity.container}"
                )
        except Exception:
            try:
                self._remove_container(spec.identity)
            except Exception:
                pass
            raise
        return ProviderStatus(
            identity=spec.identity,
            source_fingerprint=source_fingerprint,
            generation=generation,
            config_fingerprint=config_fingerprint,
            image_built=False,
            container_restarted=True,
            container_id=self._resource_id(
                started, kind="container", name=spec.identity.container
            ),
        )

    def require_startable(self, spec: ProviderSpec) -> None:
        """Fail before registry publication if start would reject current state."""

        source_fingerprint = self.require_current_image(spec)
        config_fingerprint = provider_config_fingerprint(spec, source_fingerprint)
        current = self._owned(
            spec.identity, kind="container", name=spec.identity.container
        )
        if current is None:
            return
        labels = self._resource_labels(current, kind="container")
        if (
            self._container_running(current)
            and self._container_has_no_network(current)
            and labels.get(PROVIDER_CONFIG_FINGERPRINT_LABEL)
            == config_fingerprint
        ):
            return
        raise CycloError(
            f"provider container {spec.identity.prefix!r} already exists with "
            "stale or stopped configuration; run `cyclo provider restart "
            f"{spec.identity.prefix}`"
        )

    def start(self, spec: ProviderSpec) -> ProviderStatus:
        """Start one absent provider from a current image; never builds or replaces."""

        if spec.identity.system_id != self.system_id:
            raise CycloError("provider identity belongs to a different Cyclo installation")
        source_fingerprint = self.require_current_image(spec)
        generation = provider_generation(
            spec.identity, spec.arguments, source_fingerprint
        )
        config_fingerprint = provider_config_fingerprint(spec, source_fingerprint)
        current = self._owned(
            spec.identity, kind="container", name=spec.identity.container
        )
        if current is not None:
            labels = self._resource_labels(current, kind="container")
            if (
                self._container_running(current)
                and self._container_has_no_network(current)
                and labels.get(PROVIDER_CONFIG_FINGERPRINT_LABEL)
                == config_fingerprint
            ):
                return ProviderStatus(
                    identity=spec.identity,
                    source_fingerprint=source_fingerprint,
                    generation=generation,
                    config_fingerprint=config_fingerprint,
                    image_built=False,
                    container_restarted=False,
                    container_id=self._resource_id(
                        current, kind="container", name=spec.identity.container
                    ),
                )
            raise CycloError(
                f"provider container {spec.identity.prefix!r} already exists with "
                "stale or stopped configuration; run `cyclo provider restart "
                f"{spec.identity.prefix}`"
            )
        return self._launch(spec, source_fingerprint=source_fingerprint)

    def restart(self, spec: ProviderSpec, *, build: bool = False) -> ProviderStatus:
        """Explicitly replace one provider, optionally rebuilding it first."""

        source_fingerprint = (
            self.build(spec) if build else self.require_current_image(spec)
        )
        self._remove_container(spec.identity)
        return self._launch(spec, source_fingerprint=source_fingerprint)

    def status(
        self,
        identity: ProviderIdentity,
        spec: ProviderSpec | None = None,
    ) -> ProviderContainerStatus:
        if identity.system_id != self.system_id:
            raise CycloError("provider identity belongs to a different Cyclo installation")
        image = self._owned(identity, kind="image", name=identity.image)
        container = self._owned(identity, kind="container", name=identity.container)
        image_current = False
        configuration_current = False
        if spec is not None:
            source_fingerprint = provider_source_fingerprint(spec.source)
            image_current = (
                image is not None
                and self._resource_labels(image, kind="image").get(
                    PROVIDER_SOURCE_FINGERPRINT_LABEL
                )
                == source_fingerprint
            )
            if container is not None:
                expected = provider_config_fingerprint(spec, source_fingerprint)
                configuration_current = (
                    self._resource_labels(container, kind="container").get(
                        PROVIDER_CONFIG_FINGERPRINT_LABEL
                    )
                    == expected
                )
        return ProviderContainerStatus(
            identity=identity,
            image_exists=image is not None,
            image_current=image_current,
            container_exists=container is not None,
            container_running=(
                container is not None and self._container_running(container)
            ),
            configuration_current=configuration_current,
            container_id=(
                self._resource_id(
                    container, kind="container", name=identity.container
                )
                if container is not None
                else None
            ),
        )

    def owned_identities(self) -> tuple[ProviderIdentity, ...]:
        """List explicitly managed provider identities without consulting host.conf."""

        selected: dict[str, ProviderIdentity] = {}
        for kind in ("container", "image"):
            inspect = (
                self._inspect_container if kind == "container" else self._inspect_image
            )
            for identifier in self._list_owned_ids(kind):
                info = inspect(identifier)
                if info is None:
                    continue
                identity = self._identity_from_owned_resource(
                    info, kind=kind, identifier=identifier
                )
                selected[identity.prefix] = identity
        return tuple(selected[prefix] for prefix in sorted(selected))

    @staticmethod
    def _require_image_entrypoint(info: Mapping[str, object], image: str) -> None:
        config = info.get("Config")
        entrypoint = config.get("Entrypoint") if isinstance(config, Mapping) else None
        if (
            not isinstance(entrypoint, list)
            or not entrypoint
            or any(not isinstance(value, str) or not value for value in entrypoint)
        ):
            raise CycloError(f"provider image must define OCI ENTRYPOINT: {image}")

    @staticmethod
    def _container_running(info: Mapping[str, object]) -> bool:
        state = info.get("State")
        return isinstance(state, Mapping) and state.get("Running") is True

    @staticmethod
    def _container_has_no_network(info: Mapping[str, object]) -> bool:
        host_config = info.get("HostConfig")
        return (
            isinstance(host_config, Mapping)
            and host_config.get("NetworkMode") == "none"
        )

    def _remove_container(self, identity: ProviderIdentity) -> bool:
        info = self._owned(identity, kind="container", name=identity.container)
        if info is None:
            return False
        resource_id = self._resource_id(info, kind="container", name=identity.container)
        current = self._inspect_container(resource_id)
        if current is None:
            return False
        self._require_owned(current, identity, kind="container", name=identity.container)
        if self._container_running(current):
            self._run(
                ["docker", "stop", "--timeout", "10", resource_id],
                capture=True,
            )
        self._run(["docker", "rm", resource_id], capture=True)
        return True

    def container_running(self, identity: ProviderIdentity) -> bool:
        if identity.system_id != self.system_id:
            raise CycloError("provider identity belongs to a different Cyclo installation")
        info = self._owned(identity, kind="container", name=identity.container)
        return info is not None and self._container_running(info)

    def logs_tail(self, identity: ProviderIdentity, lines: int = 40) -> str:
        if identity.system_id != self.system_id:
            raise CycloError("provider identity belongs to a different Cyclo installation")
        if not isinstance(lines, int) or not 1 <= lines <= 10_000:
            raise CycloError("provider log line count must be from 1 to 10000")
        info = self._owned(identity, kind="container", name=identity.container)
        if info is None:
            raise CycloError(f"provider container does not exist: {identity.container}")
        resource_id = self._resource_id(info, kind="container", name=identity.container)
        process = self._run(
            ["docker", "logs", "--tail", str(lines), resource_id],
            capture=True,
            check=False,
        )
        output = (process.stdout or "") + (process.stderr or "")
        if process.returncode != 0:
            raise CycloError(
                f"failed to read provider container logs: "
                f"{output.strip() or identity.container}"
            )
        return output

    @staticmethod
    def _clear_provider_socket(spec: ProviderSpec) -> None:
        _require_socket_directory(spec.provider_socket_dir, "provider socket directory")
        descriptor = -1
        try:
            descriptor = os.open(
                spec.provider_socket_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                metadata = os.stat(
                    CONTAINER_PROVIDER_SOCKET.name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISDIR(metadata.st_mode):
                raise CycloError(
                    f"provider socket path is unexpectedly a directory: "
                    f"{spec.provider_socket_dir / CONTAINER_PROVIDER_SOCKET.name}"
                )
            os.unlink(CONTAINER_PROVIDER_SOCKET.name, dir_fd=descriptor)
        except OSError as exc:
            raise CycloError(
                f"cannot clear provider socket in {spec.provider_socket_dir}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def stop(self, identity: ProviderIdentity) -> bool:
        if identity.system_id != self.system_id:
            raise CycloError("provider identity belongs to a different Cyclo installation")
        return self._remove_container(identity)

    def _list_owned_ids(self, kind: str) -> tuple[str, ...]:
        plural = {
            "container": "container",
            "image": "image",
        }[kind]
        command = [
            "docker",
            plural,
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={PROVIDER_OWNERSHIP_LABEL}={PROVIDER_OWNERSHIP_VALUE}",
            "--filter",
            f"label={PROVIDER_SYSTEM_LABEL}={self.system_id}",
        ]
        if kind == "container":
            command.insert(3, "--all")
        process = self._run(command, capture=True)
        return tuple(
            dict.fromkeys(
                line.strip() for line in process.stdout.splitlines() if line.strip()
            )
        )

    def _identity_from_owned_resource(
        self, info: Mapping[str, object], *, kind: str, identifier: str
    ) -> ProviderIdentity:
        labels = self._resource_labels(info, kind=kind)
        prefix = labels.get(PROVIDER_PREFIX_LABEL)
        if not isinstance(prefix, str):
            raise CycloError(
                f"owned Docker provider {kind} has no valid prefix: {identifier}"
            )
        identity = self.identity(prefix)
        expected_name = (
            identity.container
            if kind == "container"
            else identity.image
        )
        self._require_owned(info, identity, kind=kind, name=expected_name)
        return identity
