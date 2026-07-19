from __future__ import annotations

import hashlib
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import time
import urllib.error
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .credential_gateway import auth as gateway_auth
from .credential_gateway import gateway as gateway_runtime
from .docker import DockerContainerState, docker_container_state
from .errors import CycloError
from .gateway import CredentialGateway
from .host_config import HostConfig
from .state import StateStore


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
PROVIDER_RUNTIME_HEALTH_PATH = "/health"
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


def _container_is_operational(info: Mapping[str, object]) -> bool:
    return docker_container_state(
        info, name="provider-runtime"
    ).operational


def _system_id(state_root: Path) -> str:
    return hashlib.sha256(str(Path(state_root).resolve()).encode("utf-8")).hexdigest()[:12]


def provider_runtime_container_name(state_root: Path) -> str:
    return f"cyclo-provider-runtime-{_system_id(state_root)}"


def provider_runtime_base_url(container: str) -> str:
    return f"http://{container}:{PROVIDER_RUNTIME_PORT}"


def provider_runtime_health_url(container: str) -> str:
    return provider_runtime_base_url(container) + PROVIDER_RUNTIME_HEALTH_PATH


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
    return command


class RuntimeContainer:
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
        state = docker_container_state(current, name=self.container_name)
        if state is DockerContainerState.PAUSED:
            self._run(["docker", "unpause", resource_id])
        if state.lifecycle_active:
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
            lifecycle = docker_container_state(
                current, name=self.container_name
            )
            fingerprint = self._labels(current).get(RUNTIME_CONFIG_FINGERPRINT_LABEL)
            if lifecycle.operational and fingerprint == expected and not replace:
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
                if not lifecycle.operational:
                    raise CycloError(
                        f"provider-runtime container is {lifecycle.value} and not "
                        "operational; run `cyclo runtime restart`"
                    )
                raise CycloError(
                    "provider-runtime container exists with stale configuration; "
                    "run `cyclo runtime restart`"
                )
            self._remove_container()
        command = provider_runtime_run_command(
            config,
            config_fingerprint=expected,
            gateway_network_id=gateway_network_id,
        )
        self._run(command)
        started = self._owned_container()
        if started is None:
            raise CycloError("provider-runtime container disappeared after start")
        if not _container_is_operational(started):
            self._remove_container()
            raise CycloError(
                "provider-runtime container did not start in an operational state"
            )
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
        running = _container_is_operational(info)
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

    def _request(self, path: str, *, timeout: float = 5.0) -> object:
        token = self.admin_token()
        self.require_running()
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
                raise CycloError(f"provider runtime returned HTTP {status}")
            return json.loads(body.decode("utf-8"))
        except (
            OSError,
            http.client.HTTPException,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
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

    def probe_healthy(self, *, timeout: float = 2.0) -> None:
        """Require one exact health response from the current runtime."""

        if timeout <= 0:
            raise ValueError("health probe timeout must be positive")
        port = self.require_running()
        try:
            with gateway_runtime._open_loopback(
                f"http://127.0.0.1:{port}/health", timeout=timeout
            ) as response:
                body = response.read(4)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise CycloError(f"provider-runtime health probe failed: {exc}") from exc
        if response.status != 200 or body != b"ok\n":
            raise CycloError(
                "provider-runtime health probe returned an unexpected response"
            )

    def probe_operational(self, *, timeout: float = 2.0) -> None:
        """Require both credential gateway and provider runtime to answer."""

        if timeout <= 0:
            raise ValueError("health probe timeout must be positive")
        try:
            _gateway_id, _network_id, port = (
                self.credential_gateway.validate_running()
            )
            with gateway_runtime._open_loopback(
                f"http://127.0.0.1:{port}/health", timeout=timeout
            ) as response:
                body = response.read(4)
            if response.status != 200 or body != b"ok\n":
                raise CycloError(
                    "credential gateway health probe returned an unexpected response"
                )
        except (
            CycloError,
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise CycloError(f"credential gateway unavailable: {exc}") from exc
        try:
            self.probe_healthy(timeout=timeout)
        except CycloError as exc:
            raise CycloError(f"provider runtime unavailable: {exc}") from exc

    def wait_healthy(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.probe_healthy(timeout=min(2.0, max(0.1, deadline - time.monotonic())))
                return
            except CycloError:
                pass
            time.sleep(0.2)
        raise CycloError("provider-runtime container did not become healthy")

    def admin_token(self) -> str:
        return self._read_token(self.admin_token_file)
