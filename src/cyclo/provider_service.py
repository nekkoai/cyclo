from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import signal
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Iterable, Mapping, Sequence

from .errors import CycloError
from .gateway import CredentialGateway
from .host_config import HostConfig
from .provider_runtime import ProviderIdentity, ProviderRuntime
from .state import Instance, StateStore, validate_instance_id
from .team import Team
from .credential_gateway import auth as gateway_auth
from .credential_gateway import gateway as gateway_runtime


DEFAULT_PROVIDER_RUNTIME_IMAGE = "cyclo-provider-runtime:local"
PROVIDER_RUNTIME_PORT = 8788
PROVIDER_RUNTIME_STATE = Path("/var/lib/cyclo-provider-runtime")
PROVIDER_RUNTIME_CONFIG_DIR = Path("/etc/cyclo")
PROVIDER_RUNTIME_CONFIG_FILE = PROVIDER_RUNTIME_CONFIG_DIR / "host.conf"
PROVIDER_RUNTIME_SOCKET_DIR = Path("/run/cyclo/runtime")
PROVIDER_RUNTIME_ADMIN_SOCKET = PROVIDER_RUNTIME_SOCKET_DIR / "admin.sock"
PROVIDER_RUNTIME_PRIVATE_SOCKET_ROOT = Path("/run/cyclo/internal")
PROVIDER_RUNTIME_PROVIDER_SOCKET_ROOT = Path("/run/cyclo/providers")
PROVIDER_RUNTIME_ADMIN_TOKEN = Path("/run/secrets/cyclo-runtime-admin-token")
PROVIDER_RUNTIME_GATEWAY_TOKEN = Path("/run/secrets/cyclo-runtime-gateway-token")
PROVIDER_RUNTIME_CONTROL_RELOAD = "/_cyclo/v1/control/reload"
PROVIDER_RUNTIME_CONTROL_REFRESH_CATALOG = (
    "/_cyclo/v1/control/refresh-catalog"
)

RUNTIME_OWNERSHIP_LABEL = "cyclo.provider-runtime"
RUNTIME_OWNERSHIP_VALUE = "1"
RUNTIME_SYSTEM_LABEL = "cyclo.provider-runtime-system"
RUNTIME_RESOURCE_LABEL = "cyclo.provider-runtime-resource"
RUNTIME_SOURCE_FINGERPRINT_LABEL = "cyclo.source-fingerprint"
RUNTIME_CONFIG_FINGERPRINT_LABEL = "cyclo.provider-runtime-config-fingerprint"

RUNTIME_MEMORY_LIMIT = "1g"
RUNTIME_CPU_LIMIT = "2"

_MISSING_CONTAINER = ("no such container", "no such object")
_MISSING_IMAGE = ("no such image", "no such object")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, *, timeout: float) -> None:
        super().__init__("cyclo-provider-runtime", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self.socket_path)
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def _unix_http_request(
    socket_path: Path,
    method: str,
    path: str,
    *,
    token: str,
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes]:
    directory = -1
    connection: _UnixHTTPConnection | None = None
    try:
        directory = os.open(
            socket_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        # The Cyclo state root may have a pathname longer than AF_UNIX's
        # sun_path limit. Resolve the already-open, non-symlink directory
        # through procfs so connect(2) receives a short stable pathname.
        selected = Path(f"/proc/self/fd/{directory}") / socket_path.name
        connection = _UnixHTTPConnection(selected, timeout=timeout)
        connection.request(
            method,
            path,
            body=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        if connection is not None:
            connection.close()
        if directory >= 0:
            os.close(directory)


class _RuntimeStaleAfterAcknowledgement(CycloError):
    """A control transaction committed before its runtime became stale."""


@contextmanager
def _defer_termination_signals() -> Iterator[None]:
    """Finish one file-to-ack authority transition before honoring termination."""

    selected = {
        candidate
        for name in ("SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM")
        if (candidate := getattr(signal, name, None)) is not None
    }
    if not selected or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, selected)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@dataclass(frozen=True)
class RuntimeClient:
    project_id: str
    name: str
    generation: str
    kind: str = "team"
    provider_prefix: str | None = None


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    image: str
    container: str
    gateway_container: str
    gateway_network: str
    host_config: Path | None
    state_root: Path
    runtime_socket_dir: Path
    provider_socket_root: Path
    admin_token_file: Path
    gateway_token_file: Path


@dataclass(frozen=True)
class ProviderRuntimeStatus:
    exists: bool
    running: bool
    current: bool
    container_id: str | None


def _system_id(state_root: Path) -> str:
    return hashlib.sha256(str(Path(state_root).resolve()).encode("utf-8")).hexdigest()[:12]


def provider_runtime_container_name(state_root: Path) -> str:
    return f"cyclo-provider-runtime-{_system_id(state_root)}"


def provider_runtime_base_url(container: str) -> str:
    return f"http://{container}:{PROVIDER_RUNTIME_PORT}"


def provider_runtime_context_root() -> Path:
    return Path(__file__).with_name("provider_runtime_context")


def _fingerprint_tree(root: Path) -> str:
    selected = root.resolve()
    if not selected.is_dir():
        raise CycloError(f"provider-runtime build context not found: {selected}")
    digest = hashlib.sha256()
    digest.update(b"cyclo-provider-runtime-source-v1\0")
    for path in sorted(selected.rglob("*"), key=lambda item: item.relative_to(selected).as_posix()):
        relative = path.relative_to(selected)
        if any(part in {".git", "node_modules", "__pycache__"} for part in relative.parts):
            continue
        metadata = path.lstat()
        digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"link\0")
            digest.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            digest.update(b"x" if metadata.st_mode & 0o111 else b"-")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"dir\0")
        digest.update(b"\0")
    return digest.hexdigest()


def provider_runtime_source_fingerprint() -> str:
    return _fingerprint_tree(provider_runtime_context_root())


def provider_runtime_build_command(image: str, fingerprint: str) -> list[str]:
    root = provider_runtime_context_root().resolve()
    return [
        "docker",
        "build",
        "-t",
        image,
        "--label",
        f"{RUNTIME_SOURCE_FINGERPRINT_LABEL}={fingerprint}",
        "-f",
        str(root / "Dockerfile"),
        str(root),
    ]


def _bind(source: Path, target: Path, *, readonly: bool) -> str:
    if "," in str(source):
        raise CycloError(f"Docker bind source cannot contain a comma: {source}")
    return (
        f"type=bind,src={source},dst={target}"
        + (",readonly" if readonly else "")
    )


def _token_hash(path: Path) -> str:
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CycloError(f"cannot read provider-runtime capability {path}: {exc}") from exc
    if not value:
        raise CycloError(f"provider-runtime capability is empty: {path}")
    return hashlib.sha256(value).hexdigest()


def provider_runtime_private_socket_dir(config: ProviderRuntimeConfig) -> Path:
    """Return a runtime-only mount point unknown to provider containers."""

    capability_id = _token_hash(config.admin_token_file)[:32]
    return PROVIDER_RUNTIME_PRIVATE_SOCKET_ROOT / capability_id


def _canonical_host_config(path: Path) -> Path | None:
    """Return a real file suitable for an exact Docker bind, or missing."""

    selected = Path(path)
    try:
        lexical = selected.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CycloError(f"cannot inspect host configuration {selected}: {exc}") from exc
    try:
        resolved = selected.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        if stat.S_ISLNK(lexical.st_mode):
            raise CycloError(
                f"cannot resolve host configuration symlink {selected}: {exc}"
            ) from exc
        raise CycloError(f"cannot resolve host configuration {selected}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CycloError(f"host configuration is not a regular file: {resolved}")
    return resolved


def _host_config_identity(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"state": "missing"}
    resolved = _canonical_host_config(path)
    if resolved is None:
        return {"state": "missing"}
    try:
        metadata = resolved.stat()
        contents = resolved.read_bytes()
    except OSError as exc:
        raise CycloError(f"cannot read host configuration {resolved}: {exc}") from exc
    return {
        "state": "file",
        "path": str(resolved),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def provider_runtime_config_fingerprint(
    config: ProviderRuntimeConfig, source_fingerprint: str
) -> str:
    """Fingerprint all runtime startup inputs, including host.conf contents."""

    data = {
        "version": 3,
        "image": config.image,
        "source": source_fingerprint,
        "gateway_container": config.gateway_container,
        "gateway_network": config.gateway_network,
        # The runtime reads host.conf only at startup. Both in-place edits and
        # atomic replacements therefore make the running container stale.
        "host_config": _host_config_identity(config.host_config),
        "state_root": str(config.state_root),
        "runtime_socket_dir": str(config.runtime_socket_dir),
        "provider_socket_root": str(config.provider_socket_root),
        "admin_token_sha256": _token_hash(config.admin_token_file),
        "gateway_token_sha256": _token_hash(config.gateway_token_file),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "memory": RUNTIME_MEMORY_LIMIT,
        "cpus": RUNTIME_CPU_LIMIT,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def provider_runtime_run_command(
    config: ProviderRuntimeConfig,
    *,
    source_fingerprint: str,
    config_fingerprint: str,
    gateway_network_id: str,
) -> list[str]:
    private_socket_dir = provider_runtime_private_socket_dir(config)
    private_admin_socket = private_socket_dir / PROVIDER_RUNTIME_ADMIN_SOCKET.name
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        config.container,
        "--label",
        f"{RUNTIME_OWNERSHIP_LABEL}={RUNTIME_OWNERSHIP_VALUE}",
        "--label",
        f"{RUNTIME_SYSTEM_LABEL}={_system_id(config.state_root)}",
        "--label",
        f"{RUNTIME_RESOURCE_LABEL}={config.container}",
        "--label",
        f"{RUNTIME_CONFIG_FINGERPRINT_LABEL}={config_fingerprint}",
        "--restart",
        "unless-stopped",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--stop-timeout",
        "10",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        "256",
        "--ulimit",
        "nofile=2048:2048",
        "--memory",
        RUNTIME_MEMORY_LIMIT,
        "--memory-swap",
        RUNTIME_MEMORY_LIMIT,
        "--cpus",
        RUNTIME_CPU_LIMIT,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--network",
        gateway_network_id,
        "--network-alias",
        config.container,
        "--publish",
        f"127.0.0.1::{PROVIDER_RUNTIME_PORT}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_PORT={PROVIDER_RUNTIME_PORT}",
        "-e",
        f"CYCLO_HOST_CONFIG={PROVIDER_RUNTIME_CONFIG_FILE}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_STATE={PROVIDER_RUNTIME_STATE}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_SOCKET_ROOT={private_socket_dir}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_ADMIN_SOCKET={private_admin_socket}",
        "-e",
        f"CYCLO_PROVIDER_SOCKET_ROOT={PROVIDER_RUNTIME_PROVIDER_SOCKET_ROOT}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_ADMIN_TOKEN_FILE={PROVIDER_RUNTIME_ADMIN_TOKEN}",
        "-e",
        f"CYCLO_GATEWAY_TOKEN_FILE={PROVIDER_RUNTIME_GATEWAY_TOKEN}",
        "-e",
        f"CYCLO_GATEWAY_BASE_URL=http://{config.gateway_container}:8787",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_CLIENTS={PROVIDER_RUNTIME_STATE / 'clients.json'}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_EXPECTED={PROVIDER_RUNTIME_STATE / 'registry' / 'expected-providers.json'}",
        "-e",
        f"CYCLO_PROVIDER_RUNTIME_REGISTERED={PROVIDER_RUNTIME_STATE / 'registered-providers.json'}",
    ]
    host_config = (
        None
        if config.host_config is None
        else _canonical_host_config(config.host_config)
    )
    if host_config is not None:
        command.extend(
            [
                "--mount",
                _bind(host_config, PROVIDER_RUNTIME_CONFIG_FILE, readonly=True),
            ]
        )
    command.extend(
        [
            "--mount",
            _bind(config.state_root, PROVIDER_RUNTIME_STATE, readonly=False),
            "--mount",
            _bind(config.runtime_socket_dir, private_socket_dir, readonly=False),
            "--mount",
            _bind(
                config.provider_socket_root,
                PROVIDER_RUNTIME_PROVIDER_SOCKET_ROOT,
                readonly=True,
            ),
            "--mount",
            _bind(config.admin_token_file, PROVIDER_RUNTIME_ADMIN_TOKEN, readonly=True),
            "--mount",
            _bind(config.gateway_token_file, PROVIDER_RUNTIME_GATEWAY_TOKEN, readonly=True),
            config.image,
        ]
    )
    # The source label belongs to the image, not the container configuration;
    # keeping it out of config state avoids treating host.conf edits as builds.
    assert source_fingerprint
    return command


class ProviderService:
    def __init__(
        self,
        store: StateStore,
        host_config: HostConfig,
        *,
        image: str = DEFAULT_PROVIDER_RUNTIME_IMAGE,
        gateway_image: str = gateway_runtime.DEFAULT_GATEWAY_IMAGE,
        store_volume: str = gateway_runtime.DEFAULT_STORE_VOLUME,
    ) -> None:
        self.store = store
        self.host_config = host_config
        self.image = image
        self.credential_gateway = CredentialGateway(
            store,
            gateway_image=gateway_image,
            store_volume=store_volume,
        )
        self.state_root = store.provider_runtime_root
        self.container_name = provider_runtime_container_name(self.state_root)
        self.gateway_container = gateway_runtime.gateway_container_name(
            store.gateway_registry
        )
        self.gateway_network = gateway_runtime.gateway_network_name(
            store.gateway_registry
        )
        self.runtime_socket_dir = self.state_root / "sockets" / "runtime"
        self.admin_socket_file = (
            self.runtime_socket_dir / PROVIDER_RUNTIME_ADMIN_SOCKET.name
        )
        self.provider_socket_root = self.state_root / "sockets" / "providers"
        self.admin_token_file = self.state_root / "admin.token"

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

    def _inspect(self, kind: str, name: str) -> dict[str, object] | None:
        missing = _MISSING_IMAGE if kind == "image" else _MISSING_CONTAINER
        process = self._run(
            ["docker", kind, "inspect", name], capture=True, check=False
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").lower()
            if any(marker in detail for marker in missing):
                return None
            raise CycloError(f"cannot inspect Docker {kind} {name}: {detail.strip()}")
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CycloError(f"cannot inspect Docker {kind}: {name}") from exc
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise CycloError(f"cannot inspect Docker {kind}: {name}")
        return value[0]

    @staticmethod
    def _labels(info: Mapping[str, object]) -> Mapping[str, object]:
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        return labels if isinstance(labels, Mapping) else {}

    @staticmethod
    def _resource_id(info: Mapping[str, object], name: str) -> str:
        value = info.get("Id")
        if not isinstance(value, str) or not value:
            raise CycloError(f"cannot inspect provider-runtime container ID: {name}")
        return value

    def _owned_container(self) -> dict[str, object] | None:
        info = self._inspect("container", self.container_name)
        if info is None:
            return None
        labels = self._labels(info)
        name = info.get("Name")
        if isinstance(name, str) and name.startswith("/"):
            name = name[1:]
        if (
            name != self.container_name
            or labels.get(RUNTIME_OWNERSHIP_LABEL) != RUNTIME_OWNERSHIP_VALUE
            or labels.get(RUNTIME_SYSTEM_LABEL) != _system_id(self.state_root)
            or labels.get(RUNTIME_RESOURCE_LABEL) != self.container_name
        ):
            raise CycloError(
                f"Docker container name is owned outside this Cyclo provider runtime: "
                f"{self.container_name}"
            )
        self._resource_id(info, self.container_name)
        return info

    def _image_current(self, source_fingerprint: str) -> bool:
        info = self._inspect("image", self.image)
        if info is None:
            return False
        labels = self._labels(info)
        return labels.get(RUNTIME_SOURCE_FINGERPRINT_LABEL) == source_fingerprint

    def _prepare_layout(self) -> None:
        if self.state_root.is_symlink():
            raise CycloError(
                f"refusing symlinked provider-runtime state root: {self.state_root}"
            )
        self.store.ensure()
        try:
            resolved_config = self.host_config.path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise CycloError(
                f"cannot resolve host configuration path {self.host_config.path}: {exc}"
            ) from exc
        if resolved_config == self.state_root or resolved_config.is_relative_to(
            self.state_root
        ):
            raise CycloError(
                "host configuration must not be inside writable provider-runtime "
                f"state: {self.host_config.path}"
            )
        for path, mode in (
            (self.runtime_socket_dir, 0o700),
            (self.provider_socket_root, 0o755),
        ):
            if path.is_symlink():
                raise CycloError(f"refusing symlinked provider-runtime state: {path}")
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        self._stable_token(self.admin_token_file)
        gateway_runtime.shared_token(self.store.gateway_registry)

    @staticmethod
    def _read_token(path: Path) -> str:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise CycloError(f"cannot read provider-runtime token {path}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise CycloError(
                f"provider-runtime token is not a regular file: {path}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CycloError(f"provider-runtime token must have mode 0600: {path}")
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise CycloError(
                    f"provider-runtime token changed while reading: {path}"
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(64 * 1024 + 1)
        except OSError as exc:
            raise CycloError(f"cannot read provider-runtime token {path}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not raw or len(raw) > 64 * 1024:
            raise CycloError(f"provider-runtime token has an invalid size: {path}")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise CycloError(f"provider-runtime token is not UTF-8: {path}") from exc
        if not value or any(character.isspace() for character in value):
            raise CycloError(f"provider-runtime token is malformed: {path}")
        return value

    @classmethod
    def _stable_token(cls, path: Path) -> str:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise CycloError(
                f"refusing unsafe provider-runtime token directory: {path.parent}"
            )
        os.chmod(path.parent, 0o700)
        for _attempt in range(100):
            if path.exists() or path.is_symlink():
                return cls._read_token(path)
            value = gateway_auth.make_proxy_token()
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                time.sleep(0.01)
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(value + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            return value
        raise CycloError(f"timed out creating provider-runtime token: {path}")

    def config(self) -> ProviderRuntimeConfig:
        return ProviderRuntimeConfig(
            image=self.image,
            container=self.container_name,
            gateway_container=self.gateway_container,
            gateway_network=self.gateway_network,
            # Docker receives only the canonical file, never its writable parent.
            # A genuinely absent file means gateway-only operation and no bind.
            host_config=_canonical_host_config(self.host_config.path),
            state_root=self.state_root,
            runtime_socket_dir=self.runtime_socket_dir,
            provider_socket_root=self.provider_socket_root,
            admin_token_file=self.admin_token_file,
            gateway_token_file=self.store.gateway_registry / "gateway-token",
        )

    def build(self) -> None:
        fingerprint = provider_runtime_source_fingerprint()
        self._run(provider_runtime_build_command(self.image, fingerprint))
        if not self._image_current(fingerprint):
            raise CycloError("provider-runtime image has the wrong source fingerprint")

    def _require_current_image(self) -> str:
        fingerprint = provider_runtime_source_fingerprint()
        if not self._image_current(fingerprint):
            raise CycloError(
                "provider-runtime image is missing or stale; run "
                "`cyclo runtime restart --build`"
            )
        return fingerprint

    def _remove_container(self) -> bool:
        info = self._owned_container()
        if info is None:
            return False
        resource_id = self._resource_id(info, self.container_name)
        current = self._inspect("container", resource_id)
        if current is None:
            return False
        # Revalidate ownership by name immediately before mutation.
        checked = self._owned_container()
        if checked is None or self._resource_id(checked, self.container_name) != resource_id:
            raise CycloError("provider-runtime container changed before removal")
        state = current.get("State")
        if isinstance(state, Mapping) and state.get("Running") is True:
            self._run(["docker", "stop", "--timeout", "10", resource_id])
        self._run(["docker", "rm", resource_id])
        return True

    def start(self, *, build: bool = False, replace: bool = False) -> None:
        self._prepare_layout()
        if build:
            self.build()
        source_fingerprint = self._require_current_image()
        config = self.config()
        expected = provider_runtime_config_fingerprint(config, source_fingerprint)
        # Runtime lifecycle never provisions the gateway, but it refuses to
        # launch or report start success unless that credential boundary is live.
        _gateway_container_id, gateway_network_id, _gateway_port = (
            self.credential_gateway.validate_running()
        )
        current = self._owned_container()
        if current is not None:
            state = current.get("State")
            running = isinstance(state, Mapping) and state.get("Running") is True
            fingerprint = self._labels(current).get(RUNTIME_CONFIG_FINGERPRINT_LABEL)
            if running and fingerprint == expected and not replace:
                settings = current.get("NetworkSettings")
                networks = (
                    settings.get("Networks")
                    if isinstance(settings, Mapping)
                    else None
                )
                attached = isinstance(networks, Mapping) and any(
                    isinstance(network, Mapping)
                    and network.get("NetworkID") == gateway_network_id
                    for network in networks.values()
                )
                if not attached:
                    self._run(
                        [
                            "docker",
                            "network",
                            "connect",
                            "--alias",
                            self.container_name,
                            gateway_network_id,
                            self._resource_id(current, self.container_name),
                        ]
                    )
                return
            if not replace:
                raise CycloError(
                    "provider-runtime container exists with stale configuration; "
                    "run `cyclo runtime restart`"
                )
            self._remove_container()
        command = provider_runtime_run_command(
            config,
            source_fingerprint=source_fingerprint,
            config_fingerprint=expected,
            gateway_network_id=gateway_network_id,
        )
        self._run(command)
        started = self._owned_container()
        if started is None:
            raise CycloError("provider-runtime container disappeared after start")
        state = started.get("State")
        if not isinstance(state, Mapping) or state.get("Running") is not True:
            self._remove_container()
            raise CycloError("provider-runtime container did not start")
        try:
            self.wait_healthy()
        except Exception:
            self._remove_container()
            raise

    def restart(self, *, build: bool = False) -> None:
        self.start(build=build, replace=True)

    def stop(self) -> bool:
        return self._remove_container()

    def status(self) -> ProviderRuntimeStatus:
        info = self._owned_container()
        if info is None:
            return ProviderRuntimeStatus(False, False, False, None)
        state = info.get("State")
        running = isinstance(state, Mapping) and state.get("Running") is True
        source = ""
        try:
            source = provider_runtime_source_fingerprint()
            expected = provider_runtime_config_fingerprint(self.config(), source)
        except CycloError:
            expected = ""
        settings = info.get("NetworkSettings")
        networks = (
            settings.get("Networks") if isinstance(settings, Mapping) else None
        )
        gateway_network_current = (
            isinstance(networks, Mapping)
            and self.gateway_network in networks
        )
        current = (
            self._labels(info).get(RUNTIME_CONFIG_FINGERPRINT_LABEL) == expected
            and bool(source)
            and self._image_current(source)
            and gateway_network_current
        )
        return ProviderRuntimeStatus(
            True,
            running,
            current,
            self._resource_id(info, self.container_name),
        )

    def _runtime_port(self, *, require_current: bool) -> int:
        status = self.status()
        if not status.running:
            raise CycloError(
                "provider runtime is not running; run `cyclo runtime start`"
            )
        if require_current and not status.current:
            raise CycloError(
                "provider runtime configuration or image is stale; run "
                "`cyclo runtime restart` (add `--build` only if Cyclo reports "
                "that the image is stale)"
            )
        process = self._run(
            ["docker", "port", status.container_id or self.container_name, f"{PROVIDER_RUNTIME_PORT}/tcp"],
            capture=True,
        )
        line = process.stdout.strip().splitlines()[-1] if process.stdout.strip() else ""
        try:
            return int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise CycloError(f"unexpected Docker port output: {line!r}") from exc

    def require_running(self) -> int:
        return self._runtime_port(require_current=True)

    def _request(self, path: str, token: str, *, timeout: float = 5.0) -> object:
        port = self.require_running()
        if hmac.compare_digest(token, self.admin_token()):
            try:
                status, body = _unix_http_request(
                    self.admin_socket_file,
                    "GET",
                    path,
                    token=token,
                    body=None,
                    timeout=timeout,
                )
                if status < 200 or status >= 300:
                    raise CycloError(
                        f"provider runtime returned HTTP {status}"
                    )
                return json.loads(body.decode("utf-8"))
            except (OSError, http.client.HTTPException, UnicodeError, json.JSONDecodeError) as exc:
                raise CycloError(f"failed to query provider runtime: {exc}") from exc
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with gateway_runtime._open_loopback(
                request, timeout=timeout
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise CycloError(f"failed to query provider runtime: {exc}") from exc

    def _control_request_once(
        self,
        path: str,
        operation: str,
        *,
        require_current: bool,
        timeout: float,
    ) -> None:
        # Revocation must still reach a running process whose host.conf
        # fingerprint is stale. If an older image lacks this control protocol,
        # the acknowledged reload fails and the caller applies its fail-closed
        # policy.
        self._runtime_port(require_current=require_current)
        try:
            status, body = _unix_http_request(
                self.admin_socket_file,
                "POST",
                path,
                token=self.admin_token(),
                body=b"",
                timeout=timeout,
            )
        except (OSError, http.client.HTTPException) as exc:
            raise CycloError(
                f"provider-runtime {operation} request failed: {exc}"
            ) from exc
        if status != 204 or body:
            detail = f"HTTP {status}"
            if body:
                detail += " with an unexpected response body"
            raise CycloError(
                f"provider-runtime {operation} was not acknowledged: {detail}"
            )
        if require_current:
            try:
                self._runtime_port(require_current=True)
            except CycloError as exc:
                raise _RuntimeStaleAfterAcknowledgement(
                    f"provider-runtime {operation} was acknowledged while the "
                    "runtime became stale"
                ) from exc

    def capability_update_guard(self):
        """Defer process termination across one registry-write/control-ack pair."""

        return _defer_termination_signals()

    def _apply_control(
        self,
        path: str,
        operation: str,
        *,
        stop_on_failure: bool,
        require_current: bool,
        attempts: int = 3,
        timeout: float = 2.0,
        retry_delay: float = 0.1,
    ) -> None:
        if attempts < 1:
            raise ValueError("control attempts must be positive")
        problem: CycloError | None = None
        for attempt in range(attempts):
            try:
                self._control_request_once(
                    path,
                    operation,
                    require_current=require_current,
                    timeout=timeout,
                )
                return
            except _RuntimeStaleAfterAcknowledgement as exc:
                # The control operation has already committed. Retrying would
                # leave newly activated authority live on stale routing during
                # each retry delay, so apply the failure policy immediately.
                problem = exc
                break
            except CycloError as exc:
                problem = exc
            except BaseException:
                if stop_on_failure:
                    try:
                        self.stop()
                    except Exception as stop_error:
                        raise CycloError(
                            f"provider-runtime {operation} was interrupted and "
                            f"emergency stop failed: {stop_error}"
                        ) from stop_error
                raise
            if attempt + 1 < attempts:
                time.sleep(retry_delay)
        assert problem is not None
        acknowledged_stale = isinstance(
            problem, _RuntimeStaleAfterAcknowledgement
        )
        if not stop_on_failure:
            if acknowledged_stale:
                raise CycloError(
                    f"provider-runtime {operation} completed, but the runtime "
                    f"became stale immediately afterward: {problem}"
                ) from problem
            raise CycloError(
                f"provider-runtime {operation} could not be acknowledged: {problem}"
            ) from problem
        try:
            self.stop()
        except Exception as stop_error:
            circumstance = (
                "became stale after acknowledgement"
                if acknowledged_stale
                else "could not be acknowledged"
            )
            raise CycloError(
                f"provider-runtime {operation} {circumstance} "
                f"({problem}), and emergency stop failed: {stop_error}"
            ) from stop_error
        if acknowledged_stale:
            raise CycloError(
                f"provider runtime was stopped because {operation} was "
                f"acknowledged immediately before the runtime became stale: {problem}"
            ) from problem
        raise CycloError(
            f"provider runtime was stopped because {operation} could not be "
            f"acknowledged: {problem}"
        ) from problem

    def reload_control(
        self,
        *,
        require_current: bool = True,
        attempts: int = 3,
        timeout: float = 2.0,
        retry_delay: float = 0.1,
    ) -> None:
        """Apply mounted registries or remove the runtime if no ack is received."""

        self._apply_control(
            PROVIDER_RUNTIME_CONTROL_RELOAD,
            "capability activation/revocation",
            stop_on_failure=True,
            require_current=require_current,
            attempts=attempts,
            timeout=timeout,
            retry_delay=retry_delay,
        )

    def refresh_catalog_control(
        self,
        *,
        attempts: int = 3,
        timeout: float = 2.0,
        retry_delay: float = 0.1,
    ) -> None:
        """Refresh gateway models without coupling failure to runtime lifecycle."""

        self._apply_control(
            PROVIDER_RUNTIME_CONTROL_REFRESH_CATALOG,
            "catalog refresh",
            stop_on_failure=False,
            require_current=True,
            attempts=attempts,
            timeout=timeout,
            retry_delay=retry_delay,
        )

    def wait_healthy(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                port = self.require_running()
                with gateway_runtime._open_loopback(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as response:
                    if response.status == 200 and response.read() == b"ok\n":
                        return
            except (CycloError, urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        raise CycloError("provider-runtime container did not become healthy")

    def admin_token(self) -> str:
        return self._read_token(self.admin_token_file)

    @staticmethod
    def _clients(
        instances: Iterable[Instance],
    ) -> tuple[
        list[RuntimeClient],
        dict[str, tuple[str, ...]],
        dict[str, tuple[str, ...]],
    ]:
        clients: list[RuntimeClient] = []
        providers: dict[str, tuple[str, ...]] = {}
        models: dict[str, tuple[str, ...]] = {}
        for instance in instances:
            if not instance.active:
                continue
            clients.append(
                RuntimeClient(instance.id, instance.team_name, instance.generation)
            )
            providers[instance.id] = tuple(instance.providers)
            models[instance.id] = tuple(instance.models)
        return clients, providers, models

    def _team_local_addresses(
        self,
        instances: Iterable[Instance],
    ) -> dict[str, tuple[str, ...]]:
        """Return the runtime interface addresses for each private team network."""

        info = self._owned_container()
        if info is None:
            return {}
        settings = info.get("NetworkSettings")
        networks = settings.get("Networks") if isinstance(settings, Mapping) else None
        if not isinstance(networks, Mapping):
            raise CycloError("cannot inspect provider-runtime Docker networks")
        result: dict[str, tuple[str, ...]] = {}
        for instance in instances:
            network = networks.get(instance.network_name)
            if not isinstance(network, Mapping):
                result[instance.id] = ()
                continue
            addresses: list[str] = []
            for key in ("IPAddress", "GlobalIPv6Address"):
                raw = network.get(key)
                if not isinstance(raw, str) or not raw:
                    continue
                try:
                    selected = str(ipaddress.ip_address(raw))
                except ValueError as exc:
                    raise CycloError(
                        f"invalid provider-runtime address on {instance.network_name}: "
                        f"{raw!r}"
                    ) from exc
                if selected not in addresses:
                    addresses.append(selected)
            result[instance.id] = tuple(addresses)
        return result

    def _existing_provider_clients(self) -> list[dict[str, object]]:
        return [
            record
            for record in self._read_client_registry(
                self.state_root / "clients.json", "provider runtime"
            )
            if record.get("kind") == "provider"
        ]

    def merged_provider_clients(
        self, records: Iterable[Mapping[str, object]]
    ) -> tuple[dict[str, object], ...]:
        existing = self._existing_provider_clients()
        by_prefix = {
            record["provider_prefix"]: dict(record)
            for record in existing
            if isinstance(record.get("provider_prefix"), str)
        }
        order = [str(record["provider_prefix"]) for record in existing]
        for record in records:
            selected = dict(record)
            prefix = selected.get("provider_prefix")
            if not isinstance(prefix, str) or not prefix:
                raise CycloError("invalid provider-runtime provider client prefix")
            if prefix not in by_prefix:
                order.append(prefix)
            by_prefix[prefix] = selected
        return tuple(by_prefix[prefix] for prefix in order)

    def provider_client_prefixes(self) -> tuple[str, ...]:
        return tuple(
            str(record["provider_prefix"])
            for record in self._existing_provider_clients()
            if isinstance(record.get("provider_prefix"), str)
        )

    def provider_clients(self) -> tuple[dict[str, object], ...]:
        return tuple(self._existing_provider_clients())

    @staticmethod
    def _read_client_registry(
        path: Path, label: str
    ) -> list[dict[str, object]]:
        descriptor = -1
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise CycloError(
                    f"{label} client registry is not a regular file: {path}"
                )
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise CycloError(
                    f"{label} client registry changed while reading: {path}"
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(4 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise CycloError(
                f"cannot read {label} client registry {path}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > 4 * 1024 * 1024:
            raise CycloError(f"{label} client registry is too large: {path}")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CycloError(
                f"cannot read {label} client registry {path}: {exc}"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("clients"), list)
        ):
            raise CycloError(f"invalid {label} client registry: {path}")
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in document["clients"]:
            if not isinstance(raw, dict):
                raise CycloError(f"invalid {label} client registry: {path}")
            client_id = raw.get("client_id")
            if not isinstance(client_id, str) or not client_id or client_id in seen:
                raise CycloError(f"invalid {label} client registry: {path}")
            seen.add(client_id)
            records.append(dict(raw))
        return records

    @staticmethod
    def _write_client_registry(
        path: Path,
        records: Iterable[Mapping[str, object]],
        *,
        public_hashes: bool = False,
    ) -> None:
        directory_mode = 0o755 if public_hashes else 0o700
        file_mode = 0o644 if public_hashes else 0o600
        path.parent.mkdir(parents=True, mode=directory_mode, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise CycloError(
                f"refusing unsafe client registry directory: {path.parent}"
            )
        os.chmod(path.parent, directory_mode)
        document = {
            "version": 1,
            "clients": sorted(
                (dict(record) for record in records),
                key=lambda record: str(record.get("client_id", "")),
            ),
        }
        temporary = path.with_name(
            f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                file_mode,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
                stream.flush()
                # Apply the final mode before publication. After os.replace(),
                # no fallible operation may turn a successful authority change
                # into an unacknowledged error.
                os.fchmod(stream.fileno(), file_mode)
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise CycloError(f"cannot publish client registry {path}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _concrete_model_resolver(self):
        definitions = {
            definition.prefix: definition for definition in self.host_config.load()
        }

        def resolve(requested: Iterable[str]) -> tuple[str, ...]:
            resolved: list[str] = []
            visiting: set[str] = set()

            def expand(model: str) -> None:
                prefix, separator, model_id = model.partition("/")
                if not separator or not prefix or not model_id:
                    raise CycloError(f"invalid runtime model scope: {model!r}")
                definition = definitions.get(prefix)
                if definition is None:
                    if model not in resolved:
                        resolved.append(model)
                    return
                if prefix in visiting:
                    raise CycloError(
                        f"cyclic host provider dependency while resolving {model!r}"
                    )
                visiting.add(prefix)
                try:
                    for provider_input in definition.inputs:
                        expand(provider_input)
                finally:
                    visiting.remove(prefix)

            for model in requested:
                if not isinstance(model, str):
                    raise CycloError("invalid non-string runtime model scope")
                expand(model)
            return tuple(resolved)

        return resolve

    def _gateway_client_record(
        self,
        record: Mapping[str, object],
        resolve_models,
    ) -> dict[str, object]:
        models = record.get("models")
        if not isinstance(models, list) or any(
            not isinstance(model, str) for model in models
        ):
            raise CycloError("invalid provider-runtime client model scopes")
        concrete = resolve_models(models)
        selected = dict(record)
        selected.pop("local_addresses", None)
        selected["models"] = list(concrete)
        selected["providers"] = list(
            dict.fromkeys(model.partition("/")[0] for model in concrete)
        )
        return selected

    @staticmethod
    def _bridge_client_records(
        old_runtime: Iterable[Mapping[str, object]],
        new_runtime: Iterable[Mapping[str, object]],
        old_gateway: Iterable[Mapping[str, object]],
        new_gateway: Iterable[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        """Keep only capabilities safe on both sides of a registry transition."""

        old_gateway_by_id = {
            str(record.get("client_id")): dict(record) for record in old_gateway
        }
        new_gateway_by_id = {
            str(record.get("client_id")): dict(record) for record in new_gateway
        }
        new_runtime_by_id = {
            str(record.get("client_id")): dict(record) for record in new_runtime
        }
        bridge: list[dict[str, object]] = []
        for raw in old_runtime:
            record = dict(raw)
            client_id = str(record.get("client_id"))
            if new_runtime_by_id.get(client_id) != record:
                continue
            # Provider-component capabilities terminate at the runtime. A team
            # capability is safe to retain only if its concrete gateway grant is
            # also byte-for-byte structurally unchanged.
            if record.get("kind") == "team" and (
                old_gateway_by_id.get(client_id)
                != new_gateway_by_id.get(client_id)
            ):
                continue
            bridge.append(record)
        return bridge

    @staticmethod
    def _validate_combined_clients(
        teams: Iterable[Mapping[str, object]],
        providers: Iterable[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        combined = [dict(record) for record in (*tuple(teams), *tuple(providers))]
        seen: set[str] = set()
        for record in combined:
            client_id = record.get("client_id")
            token_hash = record.get("token_sha256")
            if (
                not isinstance(client_id, str)
                or not client_id
                or client_id in seen
                or not isinstance(token_hash, str)
                or len(token_hash) != 64
                or any(character not in "0123456789abcdef" for character in token_hash)
                or not isinstance(record.get("providers"), list)
                or not isinstance(record.get("models"), list)
            ):
                raise CycloError("invalid provider-runtime client record")
            seen.add(client_id)
        return combined

    def update_clients(
        self,
        instances: Iterable[Instance],
        *,
        provider_clients: Iterable[Mapping[str, object]] | None = None,
        apply_runtime: bool = True,
    ) -> dict[str, str]:
        selected_instances = tuple(instances)
        clients, providers, models = self._clients(selected_instances)
        local_addresses = (
            self._team_local_addresses(selected_instances)
            if apply_runtime
            else {}
        )
        tokens: dict[str, str] = {}
        document: list[dict[str, object]] = []
        for client in sorted(clients, key=lambda item: item.project_id):
            # The raw capability remains only in private runtime state. Its hash
            # is published to both boundaries because direct leaf requests keep
            # the original team bearer when runtime forwards them to gateway.
            identifier = validate_instance_id(client.project_id)
            token = self._stable_token(
                self.state_root / "client-tokens" / f"{identifier}.token"
            )
            tokens[client.project_id] = token
            document.append(
                {
                    "client_id": client.project_id,
                    "kind": "team",
                    "provider_prefix": None,
                    "team_id": client.name,
                    "binding_generation": client.generation or None,
                    "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "providers": list(providers[client.project_id]),
                    "models": list(models[client.project_id]),
                    "local_addresses": list(
                        local_addresses.get(client.project_id, ())
                    ),
                    "expires_at": None,
                    "enabled": True,
                    "revoked": False,
                }
            )
        retained_runtime = (
            self._existing_provider_clients()
            if provider_clients is None
            else [dict(record) for record in provider_clients]
        )
        for record in retained_runtime:
            if record.get("kind") != "provider":
                raise CycloError("invalid provider-runtime provider client record")
        runtime_records = self._validate_combined_clients(
            document, retained_runtime
        )
        gateway_path = gateway_runtime.host_client_registry_path(
            self.store.gateway_registry
        )
        resolve_models = self._concrete_model_resolver()
        gateway_teams = [
            self._gateway_client_record(record, resolve_models)
            for record in document
        ]
        gateway_records = self._validate_combined_clients(
            gateway_teams, ()
        )
        runtime_path = self.state_root / "clients.json"
        old_runtime = self._read_client_registry(
            runtime_path, "provider runtime"
        )
        old_gateway = self._read_client_registry(
            gateway_path, "credential gateway"
        )
        bridge_records = self._bridge_client_records(
            old_runtime,
            runtime_records,
            old_gateway,
            gateway_records,
        )
        # Three-phase publication fails closed at every interruption point:
        # revoke old/changed runtime grants, publish the final gateway grants,
        # then let the runtime accept new/changed capabilities.
        if apply_runtime:
            with self.capability_update_guard():
                self._write_client_registry(runtime_path, bridge_records)
                self.reload_control(require_current=False)
        else:
            self._write_client_registry(runtime_path, bridge_records)
        self._write_client_registry(
            gateway_path, gateway_records, public_hashes=True
        )
        if apply_runtime:
            with self.capability_update_guard():
                self._write_client_registry(runtime_path, runtime_records)
                self.reload_control()
        else:
            self._write_client_registry(runtime_path, runtime_records)
        return tokens

    def remove_provider_clients(self, prefixes: Iterable[str]) -> None:
        removed = set(prefixes)
        runtime_path = self.state_root / "clients.json"
        runtime_records = [
            record
            for record in self._read_client_registry(
                runtime_path, "provider runtime"
            )
            if not (
                record.get("kind") == "provider"
                and record.get("provider_prefix") in removed
            )
        ]
        checked_runtime = self._validate_combined_clients(
            (record for record in runtime_records if record.get("kind") != "provider"),
            (record for record in runtime_records if record.get("kind") == "provider"),
        )
        with self.capability_update_guard():
            self._write_client_registry(runtime_path, checked_runtime)
            self.reload_control(require_current=False)

    def rotate_client_token(self, identifier: str) -> None:
        identifier = validate_instance_id(identifier)
        path = self.state_root / "client-tokens" / f"{identifier}.token"
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CycloError(
                f"cannot rotate provider-runtime capability for {identifier}: {exc}"
            ) from exc

    def catalog(
        self,
        token: str | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, dict]:
        if refresh:
            self.refresh_catalog_control()
        data = self._request("/providers", token or self.admin_token())
        if not isinstance(data, dict):
            raise CycloError("provider runtime catalog was not a JSON object")
        return data  # type: ignore[return-value]

    @staticmethod
    def _instance_catalog(
        instance: Instance,
        catalog: Mapping[str, object],
    ) -> dict[str, dict]:
        allowed_providers = set(instance.providers)
        allowed_models = set(instance.models)
        selected: dict[str, dict] = {}
        for prefix, raw in catalog.items():
            if "*" not in allowed_providers and prefix not in allowed_providers:
                continue
            if not isinstance(raw, Mapping) or not isinstance(raw.get("models"), list):
                continue
            models = [
                model
                for model in raw["models"]
                if isinstance(model, Mapping)
                and isinstance(model.get("id"), str)
                and (
                    "*" in allowed_models
                    or f"{prefix}/{model['id']}" in allowed_models
                )
            ]
            if not models:
                continue
            selected[prefix] = {**raw, "models": models}
        return selected

    @staticmethod
    def _registered_timestamp(value: object) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if selected.tzinfo is None:
            return None
        return selected.timestamp()

    @staticmethod
    def _provider_diagnostics(
        runtime: ProviderRuntime, identity: ProviderIdentity
    ) -> str:
        try:
            status = runtime.status(identity)
            state = (
                "running"
                if status.container_running
                else "stopped"
                if status.container_exists
                else "absent"
            )
            status_detail = f"container={state}"
        except CycloError as exc:
            status_detail = f"container status unavailable: {exc}"
        try:
            logs = runtime.logs_tail(identity, lines=40).strip()
            log_detail = (
                f"; provider logs:\n{logs[-8192:]}"
                if logs
                else "; provider logs are empty"
            )
        except CycloError as exc:
            log_detail = f"; provider logs unavailable: {exc}"
        return status_detail + log_detail

    def wait_provider(
        self,
        prefix: str,
        generation: str,
        *,
        runtime: ProviderRuntime,
        identity: ProviderIdentity,
        registered_after: float | None = None,
        timeout: float = 45.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        last_problem = "no catalog entry"
        while time.monotonic() < deadline:
            try:
                container_running = runtime.container_running(identity)
            except CycloError as exc:
                last_problem = str(exc)
            else:
                if not container_running:
                    detail = self._provider_diagnostics(runtime, identity)
                    raise CycloError(
                        f"provider {prefix!r} exited before registration; {detail}"
                    )
            try:
                entry = self.catalog().get(prefix)
                if not isinstance(entry, Mapping):
                    last_problem = "no catalog entry"
                elif entry.get("kind") != "component":
                    last_problem = "catalog prefix is not a provider component"
                elif entry.get("generation") != generation:
                    last_problem = "catalog generation does not match the launched provider"
                elif registered_after is not None:
                    registered_at = self._registered_timestamp(
                        entry.get("registered_at")
                    )
                    if registered_at is None:
                        last_problem = "catalog registration has no valid timestamp"
                    elif registered_at < registered_after:
                        last_problem = "catalog registration predates this launch"
                    else:
                        return
                else:
                    return
            except CycloError as exc:
                last_problem = str(exc)
            time.sleep(0.2)
        diagnostics = self._provider_diagnostics(runtime, identity)
        raise CycloError(
            f"provider {prefix!r} did not become ready: {last_problem}; "
            f"{diagnostics}"
        )

    def prepare_instance(
        self,
        instance: Instance,
        team: Team,
        active_instances: Iterable[Instance],
    ) -> str:
        tokens = self.update_clients(active_instances)
        token = tokens.get(instance.id)
        if not token:
            raise CycloError(
                f"provider runtime did not issue a token for Cyclo instance {instance.id}"
            )
        catalog = self._instance_catalog(instance, self.catalog())
        self._validate_models(team, catalog)
        pi_root = self.store.pi_root(instance.id)
        temporary = self.store.new_tree(pi_root)
        try:
            agent_dir = temporary / "agent"
            agent_dir.mkdir(mode=0o700)
            first = team.agents[0]
            settings = {
                "defaultProvider": first.provider,
                "defaultModel": first.model_id,
                "defaultThinkingLevel": "xhigh",
                "packages": list(gateway_auth.PI_PACKAGES),
            }
            models = gateway_auth.projected_models_json(
                catalog,
                provider_runtime_base_url(self.container_name),
                token,
            )
            (agent_dir / "settings.json").write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            (agent_dir / "models.json").write_text(
                json.dumps(models, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(agent_dir / "settings.json", 0o600)
            os.chmod(agent_dir / "models.json", 0o600)
            self.store.replace_tree(temporary, pi_root)
        except Exception:
            self.store._remove_tree(temporary)
            raise
        return token

    @staticmethod
    def _validate_models(team: Team, catalog: dict[str, dict]) -> None:
        for agent in team.agents:
            provider = catalog.get(agent.provider)
            models = provider.get("models") if isinstance(provider, dict) else None
            available = {
                model["id"]
                for model in models or []
                if isinstance(model, dict)
                and isinstance(model.get("id"), str)
                and model["id"]
            }
            if agent.model_id not in available:
                raise CycloError(
                    f"agent {agent.name} requests unavailable runtime model {agent.model!r}"
                )
